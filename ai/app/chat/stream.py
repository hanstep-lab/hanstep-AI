"""Run the chat graph and turn its custom stream into INT-05 SSE events.

Order: agent.tool_call* -> agent.delta* -> agent.sources? -> [agent.metrics] -> agent.done,
or [agent.metrics] -> agent.error at any point; nothing after done/error. agent.metrics (only
with ``X-Chat-Metrics: 1``) holds the LLM calls finished so far, so failed runs are measured too.
When a later LLM call writes text after an earlier call already did (e.g. "Let me check." before
a tool call), CALL_SEPARATOR goes first in the same agent.delta, so the streamed text and
agent.done content never run two calls' text together.
The graph runs in its own task so the run deadline and a Spring disconnect (generator cancelled
or closed) cancel the graph and the in-flight LLM call. Logs carry run id, latency, token
counts, tool names and stop reasons only.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import anthropic
from fastapi.sse import ServerSentEvent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from app.chat.graph import build_graph
from app.chat.prompts import PROMPT_VERSION, system_prompt
from app.chat.schemas import ChatStreamRequest, LlmConfig
from app.chat.tools import FinanceTool

logger = logging.getLogger(__name__)

# User-facing, short and generic: no internal detail.
ERROR_MESSAGES = {
    "LLM_UPSTREAM_ERROR": "The assistant is temporarily unavailable. Please try again.",
    "AGENT_TIMEOUT": "The answer took too long. Please try again.",
    "AGENT_INTERNAL_ERROR": "Something went wrong while answering. Please try again.",
}
# Between the texts of two LLM calls (text before a tool call, then the answer).
CALL_SEPARATOR = "\n\n"
_TOOL_EVENT_FIELDS = (
    "order",
    "toolName",
    "arguments",
    "status",
    "resultRef",
    "latencyMs",
    "errorCode",
)
_CALL_METRIC_FIELDS = (
    "model",
    "stopReason",
    "inputTokens",
    "outputTokens",
    "cacheReadInputTokens",
    "cacheCreationInputTokens",
    "thinkingBlocks",
    "firstTextMs",
    "durationMs",
)


@dataclass(frozen=True)
class ChatRunOptions:
    max_tool_calls: int
    run_timeout_seconds: float
    metrics: bool = False
    model_id: str | None = None  # defaults to config.model


def build_messages(request: ChatStreamRequest) -> list[BaseMessage]:
    messages: list[BaseMessage] = [SystemMessage(system_prompt(request.language))]
    for item in request.history:
        if item.role == "USER":
            messages.append(HumanMessage(item.content))
        else:
            messages.append(AIMessage(item.content))
    messages.append(HumanMessage(request.message))
    return messages


def _event(name: str, data: dict[str, Any]) -> ServerSentEvent:
    return ServerSentEvent(
        event=name, raw_data=json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    )


def _ms(since: float) -> int:
    return round((time.perf_counter() - since) * 1000)


async def run_chat(
    request: ChatStreamRequest,
    *,
    config: LlmConfig,
    model: BaseChatModel,
    tool: FinanceTool,
    options: ChatRunOptions,
) -> AsyncIterator[ServerSentEvent]:
    started = time.perf_counter()
    graph = build_graph(model, tool, max_tool_calls=options.max_tool_calls, run_started=started)
    queue: asyncio.Queue[dict[str, Any] | BaseException | None] = asyncio.Queue()

    async def produce() -> None:
        try:
            async for item in graph.astream(
                {"messages": build_messages(request), "tool_calls_done": 0},
                stream_mode="custom",
            ):
                await queue.put(item)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await queue.put(exc)
            return
        await queue.put(None)

    task = asyncio.create_task(produce())
    deltas: list[str] = []
    tools: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    first_delta_ms: int | None = None
    # Per LLM call number: when its first text reached the stream, and how many characters.
    call_first_delta_ms: dict[int, int] = {}
    call_text_chars: dict[int, int] = {}
    last_text_call: int | None = None  # LLM call that wrote the last non-blank text
    outcome = "cancelled"

    def metrics_event() -> ServerSentEvent:
        last = calls[-1] if calls else None
        answer_call = last if last is not None and last["stopReason"] != "tool_use" else None
        return _event(
            "agent.metrics",
            {
                "config": config.to_json(PROMPT_VERSION),
                "calls": [{k: c.get(k) for k in _CALL_METRIC_FIELDS} for c in calls],
                # Run start -> first text of the final answer call (after any tool results).
                # Same clock as done.timings.firstDeltaMs; equal to it when no text came earlier.
                "firstAnswerDeltaMs": (
                    None if answer_call is None else call_first_delta_ms.get(answer_call["call"])
                ),
                # Text sent in calls that ended in a tool call (text before tools).
                "preToolTextChars": sum(
                    call_text_chars.get(c["call"], 0)
                    for c in calls
                    if c["stopReason"] == "tool_use"
                ),
            },
        )

    try:
        try:
            deadline = started + options.run_timeout_seconds
            while True:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    raise TimeoutError
                item = await asyncio.wait_for(queue.get(), remaining)
                if item is None:
                    break
                if isinstance(item, BaseException):
                    raise item
                if item["type"] == "delta":
                    now_ms = _ms(started)
                    if first_delta_ms is None:
                        first_delta_ms = now_ms
                    call_first_delta_ms.setdefault(item["call"], now_ms)
                    call_text_chars[item["call"]] = call_text_chars.get(item["call"], 0) + len(
                        item["text"]
                    )
                    text = item["text"]
                    if last_text_call is not None and item["call"] != last_text_call:
                        if not text.strip():
                            continue  # wait for the new call's first real text
                        text = CALL_SEPARATOR + text.lstrip()
                    if text.strip():
                        last_text_call = item["call"]
                    deltas.append(text)
                    yield _event("agent.delta", {"text": text})
                elif item["type"] == "tool_call":
                    tools.append(item)
                    for source in item["sources"]:
                        if source not in sources:
                            sources.append(source)
                    yield _event("agent.tool_call", {k: item[k] for k in _TOOL_EVENT_FIELDS})
                elif item["type"] == "llm_call":
                    calls.append(item)
        except TimeoutError:
            outcome = "AGENT_TIMEOUT"
        except anthropic.APIError as exc:
            logger.warning("chat llm error run=%s error=%s", request.run_id, type(exc).__name__)
            outcome = "LLM_UPSTREAM_ERROR"
        except Exception as exc:
            logger.warning("chat agent error run=%s error=%s", request.run_id, type(exc).__name__)
            outcome = "AGENT_INTERNAL_ERROR"
        else:
            content = "".join(deltas)
            if not content.strip():
                # e.g. thinking used the whole max_tokens budget
                logger.warning(
                    "chat empty answer run=%s stop_reason=%s",
                    request.run_id,
                    calls[-1]["stopReason"] if calls else None,
                )
                outcome = "AGENT_INTERNAL_ERROR"
            else:
                outcome = "done"
                if sources:
                    yield _event("agent.sources", {"sources": sources})
                if options.metrics:
                    yield metrics_event()
                total_ms = _ms(started)
                yield _event(
                    "agent.done",
                    {
                        "content": content,
                        "blocks": None,
                        "modelId": options.model_id or config.model,
                        "promptVersion": PROMPT_VERSION,
                        "latencyMs": total_ms,
                        "usage": {
                            "inputTokens": sum(c["inputTokens"] for c in calls),
                            "outputTokens": sum(c["outputTokens"] for c in calls),
                            "cacheReadInputTokens": sum(c["cacheReadInputTokens"] for c in calls),
                            "cacheCreationInputTokens": sum(
                                c["cacheCreationInputTokens"] for c in calls
                            ),
                            "llmCalls": len(calls),
                        },
                        "timings": {
                            "firstDeltaMs": first_delta_ms,
                            "toolMs": sum(t["latencyMs"] for t in tools),
                            "totalMs": total_ms,
                        },
                    },
                )
                return
        if options.metrics:
            yield metrics_event()
        yield _event("agent.error", {"code": outcome, "message": ERROR_MESSAGES[outcome]})
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        logger.info(
            "chat run=%s outcome=%s latency_ms=%s llm_calls=%s in=%s out=%s tools=%s stop=%s",
            request.run_id,
            outcome,
            _ms(started),
            len(calls),
            sum(c["inputTokens"] for c in calls),
            sum(c["outputTokens"] for c in calls),
            [t["toolName"] for t in tools],
            [c["stopReason"] for c in calls],
        )

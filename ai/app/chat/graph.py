"""LangGraph agent <-> tools loop. Nodes write stream events with ``get_stream_writer``.

Custom stream items (consumed by app.chat.stream):
- ``{"type": "delta", "text": ..., "call": n}``: a text block piece of the model output, written
  at once; ``call`` is the 0-based LLM call number in this run. Thinking blocks, progress notes
  between tools (thinking blocks under ``between_tools``) and tool_use input pieces are never
  written.
- ``{"type": "tool_call", ...}``: one per executed tool, after it finishes.
- ``{"type": "llm_call", "call": n, ...}``: usage and timing of one model call (metrics only).
"""

from __future__ import annotations

import operator
import time
from typing import Annotated, Any, TypedDict, cast

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, AnyMessage, ToolMessage
from langchain_core.messages.utils import message_chunk_to_message
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph

from app.chat.tools import ChatTool


class ChatState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    tool_calls_done: Annotated[int, operator.add]


def text_pieces(chunk: AIMessageChunk) -> list[str]:
    """Text blocks only. Anything else (thinking, tool_use, input_json_delta) stays out."""
    content = chunk.content
    if isinstance(content, str):
        return [content] if content else []
    return [
        b["text"]
        for b in content
        if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
    ]


def _usage(message: AIMessageChunk) -> dict[str, int]:
    meta: dict[str, Any] = dict(message.usage_metadata or {})
    details: dict[str, Any] = dict(meta.get("input_token_details") or {})
    cache_read = int(details.get("cache_read") or 0)
    cache_creation = int(details.get("cache_creation") or 0)
    # langchain-anthropic folds cache tokens into input_tokens; report them apart.
    return {
        "inputTokens": max(int(meta.get("input_tokens") or 0) - cache_read - cache_creation, 0),
        "outputTokens": int(meta.get("output_tokens") or 0),
        "cacheReadInputTokens": cache_read,
        "cacheCreationInputTokens": cache_creation,
    }


def _thinking_blocks(message: AIMessageChunk) -> int:
    if isinstance(message.content, str):
        return 0
    return sum(
        1
        for b in message.content
        if isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking")
    )


def build_graph(
    model: BaseChatModel, tool: ChatTool, *, max_tool_calls: int, run_started: float
) -> CompiledStateGraph[Any, Any, Any, Any]:
    with_tools = model.bind_tools(tool.schemas)
    # Tool limit reached: the messages hold tool_use blocks, so keep the definition but forbid use.
    # BaseChatModel types tool_choice as str; ChatAnthropic takes the API dict form too.
    without_tools = model.bind_tools(tool.schemas, tool_choice=cast(Any, {"type": "none"}))
    # The graph is built per run, so this counts the LLM calls of one run.
    llm_calls = 0

    async def agent(state: ChatState) -> dict[str, Any]:
        nonlocal llm_calls
        call_no = llm_calls
        llm_calls += 1
        writer = get_stream_writer()
        runnable = with_tools if state["tool_calls_done"] < max_tool_calls else without_tools
        started = time.perf_counter()
        first_text: float | None = None
        merged: AIMessageChunk | None = None
        async for raw in runnable.astream(state["messages"]):
            chunk = cast(AIMessageChunk, raw)
            for text in text_pieces(chunk):
                if first_text is None:
                    first_text = time.perf_counter()
                writer({"type": "delta", "text": text, "call": call_no})
            merged = chunk if merged is None else merged + chunk
        if merged is None:
            merged = AIMessageChunk(content="")
        # Summed chunks keep thinking blocks and their signatures untouched for the next call.
        message = message_chunk_to_message(merged)
        writer(
            {
                "type": "llm_call",
                "call": call_no,
                "model": merged.response_metadata.get("model_name"),
                "stopReason": merged.response_metadata.get("stop_reason"),
                **_usage(merged),
                "thinkingBlocks": _thinking_blocks(merged),
                "firstTextMs": None if first_text is None else round((first_text - started) * 1000),
                "durationMs": round((time.perf_counter() - started) * 1000),
                "startedMs": round((started - run_started) * 1000),
            }
        )
        return {"messages": [message]}

    async def tools(state: ChatState) -> dict[str, Any]:
        writer = get_stream_writer()
        last = state["messages"][-1]
        assert isinstance(last, AIMessage)
        results: list[ToolMessage] = []
        for call in last.tool_calls:
            outcome = await tool.run(call["name"], dict(call.get("args") or {}))
            writer(
                {
                    "type": "tool_call",
                    "order": state["tool_calls_done"] + len(results) + 1,
                    "toolName": outcome.name,
                    "arguments": outcome.arguments,
                    "status": outcome.status,
                    "resultRef": outcome.result_ref,
                    "latencyMs": outcome.latency_ms,
                    "errorCode": outcome.error_code,
                    "sources": outcome.sources,
                    "blocks": outcome.blocks,
                }
            )
            results.append(
                ToolMessage(
                    content=outcome.content,
                    tool_call_id=call["id"] or "",
                    name=outcome.name,
                    status="success" if outcome.status == "SUCCEEDED" else "error",
                )
            )
        return {"messages": results, "tool_calls_done": len(results)}

    def route(state: ChatState) -> str:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            if state["tool_calls_done"] < max_tool_calls:
                return "tools"
        return END

    graph = StateGraph(ChatState)
    graph.add_node("agent", agent)
    graph.add_node("tools", tools)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route, ["tools", END])
    graph.add_edge("tools", "agent")
    return graph.compile()

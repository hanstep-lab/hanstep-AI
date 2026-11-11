"""Fake chat model for CHAT_LLM_MODE=fake and tests. Runs the same graph as ChatAnthropic.

Behaviour:
- with tools bound and no tool result yet: one ``get_financial_analysis`` tool call;
- otherwise: an English answer that copies the numbers of the tool result, streamed word by
  word every ``delta_delay_ms``. Usage is always 0.
- ``emit_thinking``: adds a thinking block (with signature) before the tool call and the answer,
  so tests can check that thinking is filtered from deltas and kept across the tool round trip.

Test-only triggers in the user message (documented here, two at most):
- ``[fake-llm-error]``: raise an Anthropic connection error (-> LLM_UPSTREAM_ERROR).
- ``[fake-empty-answer]``: answer with no text and stop_reason ``max_tokens``.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

import anthropic
import httpx
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable
from pydantic import Field

from app.chat.tools import NO_ANALYSIS, TOOL_NAME, UNAVAILABLE

FAKE_MODEL_ID = "fake-chat-model"
TRIGGER_LLM_ERROR = "[fake-llm-error]"
TRIGGER_EMPTY_ANSWER = "[fake-empty-answer]"


def _content_text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(
        b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
    )


def answer_from_tool_result(result: str | None) -> str:
    if result is None:
        return "I can help with questions about your monthly cash flow."
    if result == UNAVAILABLE:
        return "I could not load your financial data right now. Please try again in a moment."
    data = json.loads(result)
    if data == NO_ANALYSIS:
        return (
            "There is no financial analysis for you yet. "
            "Please connect MyData so your monthly cash flow can be analyzed."
        )
    a = data["analysis"]
    return (
        f"Over the last {a['observedMonthCount']} full months, your average monthly spare money "
        f"is KRW {a['spare']:,}. Your average monthly income is KRW {a['avgMonthlyIncome']:,}, "
        f"your fixed living costs are KRW {a['fixedLiving']:,} and you send KRW "
        f"{a['remittance']:,} home. Your variable spending averages KRW "
        f"{a['variableExpense']['avg']:,} a month."
    )


class FakeChatModel(BaseChatModel):
    delta_delay_ms: int = 30
    emit_thinking: bool = False
    tools_bound: bool = False
    # Shared with copies made by bind_tools; tests read "calls" and "cancelled".
    probe: dict[str, Any] = Field(default_factory=dict)

    @property
    def _llm_type(self) -> str:
        return "fake-chat"

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: Any = None,
        **kwargs: Any,
    ) -> Runnable[Any, Any]:
        none = isinstance(tool_choice, dict) and tool_choice.get("type") == "none"
        return self.model_copy(update={"tools_bound": bool(tools) and not none})

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise NotImplementedError("FakeChatModel is async-stream only")

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        self.probe["calls"] = self.probe.get("calls", 0) + 1
        self.probe.setdefault("inputs", []).append(list(messages))
        last_human = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
        question = _content_text(messages[last_human])
        tool_results = [m for m in messages[last_human:] if isinstance(m, ToolMessage)]
        try:
            if TRIGGER_LLM_ERROR in question:
                request = httpx.Request("POST", "https://fake.invalid/v1/messages")
                # The SDK only stores the request; its own httpx fork is not needed here.
                raise anthropic.APIConnectionError(request=cast(Any, request))
            thinking: list[str | dict[Any, Any]] = (
                [{"type": "thinking", "thinking": "plan", "signature": "fake-sig", "index": 0}]
                if self.emit_thinking
                else []
            )
            if self.tools_bound and not tool_results:
                call_id = f"toolu_fake_{self.probe['calls']}"
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content=[
                            *thinking,
                            {
                                "type": "tool_use",
                                "id": call_id,
                                "name": TOOL_NAME,
                                "input": {},
                                "index": 1,
                            },
                        ],
                        tool_call_chunks=[
                            {
                                "type": "tool_call_chunk",
                                "name": TOOL_NAME,
                                "args": "{}",
                                "id": call_id,
                                "index": 1,
                            }
                        ],
                    )
                )
                yield self._final_chunk("tool_use")
                return
            if thinking:
                yield ChatGenerationChunk(message=AIMessageChunk(content=thinking))
            if TRIGGER_EMPTY_ANSWER in question:
                yield self._final_chunk("max_tokens")
                return
            last = tool_results[-1] if tool_results else None
            answer = answer_from_tool_result(None if last is None else str(last.content))
            for word in re.findall(r"\S+\s*", answer):
                await asyncio.sleep(self.delta_delay_ms / 1000)
                yield ChatGenerationChunk(
                    message=AIMessageChunk(content=[{"type": "text", "text": word, "index": 1}])
                )
            yield self._final_chunk("end_turn")
        except asyncio.CancelledError:
            self.probe["cancelled"] = True
            raise

    @staticmethod
    def _final_chunk(stop_reason: str) -> ChatGenerationChunk:
        return ChatGenerationChunk(
            message=AIMessageChunk(
                content=[],
                usage_metadata={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                response_metadata={"stop_reason": stop_reason, "model_name": FAKE_MODEL_ID},
            )
        )

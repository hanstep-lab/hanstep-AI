"""Fake chat model for CHAT_LLM_MODE=fake and tests. Runs the same graph as ChatAnthropic.

Behaviour:
- with tools bound and no tool result yet: one ``get_financial_analysis`` tool call;
- otherwise: an English answer that copies the numbers of the tool result, streamed word by
  word every ``delta_delay_ms``. Usage is always 0.
- ``emit_thinking``: adds a thinking block (with signature) before the tool call and the answer,
  so tests can check that thinking is filtered from deltas and kept across the tool round trip.
- ``pre_tool_text``: a text block streamed before the tool call (a model ignoring the prompt's
  "no text before the tool call"), so tests can measure text sent before tools.
- remittance (only when quote_remittance is bound, prompt v3): when a user message in the
  conversation asks to send money, it calls get_remittance_recipients, reads the recipient, the
  currency and the amount from the user messages (later ones win), then either asks about the
  first unclear one (recipient, amount, currency) or calls quote_remittance and answers with the
  quote amounts. Keyword rules only (see _remittance_slots); good enough for scenario tests.
- products (only when recommend_savings_products is bound, prompt v4): a question about savings,
  deposits or products (not cards or loans) calls recommend_savings_products with keyword-read
  conditions (see _product_args), then lists the returned product names or explains
  abstainReason.

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

from app.chat.product_tools import RECOMMEND_SAVINGS_PRODUCTS
from app.chat.remittance_tools import GET_REMITTANCE_RECIPIENTS, QUOTE_REMITTANCE
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


_REMIT_REQUEST = re.compile(r"\b(send|remit|transfer|transfr|snd|put)\b", re.IGNORECASE)
_AMOUNT = re.compile(r"(\d[\d,]*)(?:\s*(k)(?![a-z]))?", re.IGNORECASE)
_KRW = re.compile(r"(?<![a-z])(won|krw)\b", re.IGNORECASE)
_RECEIVE_CURRENCY = re.compile(r"(?<![a-z])(pesos?|php)\b", re.IGNORECASE)
_MOTHER = re.compile(r"\b(mom|mama|mother|nanay|lorna|bdo)\b", re.IGNORECASE)
_SELF = re.compile(r"\b(bpi|my own|mark)\b", re.IGNORECASE)


def _remittance_slots(texts: list[str]) -> dict[str, Any]:
    """Recipient relationship, currency and amount from user messages (later ones win).

    A message naming both recipients (e.g. "mom bpi") leaves the recipient unclear.
    """
    slots: dict[str, Any] = {"recipient": None, "currency": None, "amount": None}
    for text in texts:
        amount = _AMOUNT.search(text)
        if amount:
            value = int(amount.group(1).replace(",", ""))
            slots["amount"] = value * 1000 if amount.group(2) else value
        if _KRW.search(text):
            slots["currency"] = "KRW"
        elif _RECEIVE_CURRENCY.search(text):
            slots["currency"] = "PHP"
        mother, own = bool(_MOTHER.search(text)), bool(_SELF.search(text))
        if mother or own:
            slots["recipient"] = None if mother and own else ("MOTHER" if mother else "SELF")
    return slots


_PRODUCT_REQUEST = re.compile(r"\b(saving|savings|deposit|product|products)\b", re.IGNORECASE)
_NOT_PRODUCT = re.compile(r"\b(card|loan|checking)\b", re.IGNORECASE)


def _product_args(text: str) -> dict[str, Any]:
    """Conditions for recommend_savings_products from one message (keyword rules only)."""
    args: dict[str, Any] = {}
    if re.search(r"\b(deposit|lump)", text, re.IGNORECASE):
        args["productType"] = "DEPOSIT"
    elif re.search(r"\bsaving", text, re.IGNORECASE):
        args["productType"] = "SAVINGS"
    term = re.search(r"(\d+)\s*months?\s*or\s*less|within\s*(\d+)\s*months?", text, re.IGNORECASE)
    if term:
        args["maxTermMonths"] = int(term.group(1) or term.group(2))
    amount = re.search(r"(\d[\d,]*)\s*(k|thousand)?\s*(?:won|krw)", text, re.IGNORECASE)
    if amount:
        value = int(amount.group(1).replace(",", ""))
        args["monthlyAmountKrw"] = value * 1000 if amount.group(2) else value
    if re.search(r"whenever|any amount|flexible|freely", text, re.IGNORECASE):
        args["accrual"] = "FLEXIBLE"
    elif re.search(r"same (fixed )?amount|set monthly", text, re.IGNORECASE):
        args["accrual"] = "FIXED"
    if re.search(r"foreign", text, re.IGNORECASE):
        args["requireForeignerExplicit"] = True
    return args


def _product_answer(result: str) -> str:
    try:
        data = json.loads(result)
    except ValueError:
        return "I could not load the product list right now. Please try again."
    names = [p["name"] for p in data.get("products") or []]
    if names:
        return (
            f"These products fit your conditions: {', '.join(names)}. Please confirm visa and "
            "age conditions in the Shinhan app or at a branch."
        )
    reason = data.get("abstainReason")
    return {
        "REQUEST_EXCEEDS_SURPLUS": "That monthly amount is more than your monthly spare money.",
        "STAY_TOO_SHORT": "The products that fit last longer than your remaining stay.",
    }.get(reason or "", "No product fits these conditions.")


def _tool_call_chunk(name: str, args: dict[str, Any], call_id: str) -> AIMessageChunk:
    return AIMessageChunk(
        content=[{"type": "tool_use", "id": call_id, "name": name, "input": args, "index": 1}],
        tool_call_chunks=[
            {
                "type": "tool_call_chunk",
                "name": name,
                "args": json.dumps(args),
                "id": call_id,
                "index": 1,
            }
        ],
    )


def _quote_answer(result: str) -> str:
    try:
        quote = json.loads(result)
        send, receive = quote["sendAmount"], quote["receiveAmount"]
        name = quote["recipient"]["nameEn"]
    except (ValueError, KeyError, TypeError):
        return "I could not prepare this remittance right now. Please try again."
    return (
        f"{name} will receive {receive['amount']:,} {receive['currency']} for "
        f"{send['amount']:,} {send['currency']}. Please check the confirmation card and send it "
        "on the transfer screen."
    )


class FakeChatModel(BaseChatModel):
    delta_delay_ms: int = 30
    emit_thinking: bool = False
    pre_tool_text: str = ""
    tools_bound: bool = False
    bound_tool_names: tuple[str, ...] = ()
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
        names = tuple(t["name"] for t in tools if isinstance(t, dict) and "name" in t)
        return self.model_copy(
            update={"tools_bound": bool(tools) and not none, "bound_tool_names": names}
        )

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
            remittance = self._remittance_step(messages, tool_results) or self._product_step(
                question, tool_results
            )
            if remittance is not None:
                kind, value = remittance
                if kind == "tool":
                    name, args = value
                    chunk = _tool_call_chunk(name, args, f"toolu_fake_{self.probe['calls']}")
                    yield ChatGenerationChunk(message=chunk)
                    yield self._final_chunk("tool_use")
                    return
                async for piece in self._stream_text(value):
                    yield piece
                yield self._final_chunk("end_turn")
                return
            if self.tools_bound and not tool_results:
                call_id = f"toolu_fake_{self.probe['calls']}"
                # Content blocks merge by index, so the tool_use goes after the text block.
                tool_index = 2 if self.pre_tool_text else 1
                if self.pre_tool_text:
                    yield ChatGenerationChunk(
                        message=AIMessageChunk(
                            content=[{"type": "text", "text": self.pre_tool_text, "index": 1}]
                        )
                    )
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content=[
                            *thinking,
                            {
                                "type": "tool_use",
                                "id": call_id,
                                "name": TOOL_NAME,
                                "input": {},
                                "index": tool_index,
                            },
                        ],
                        tool_call_chunks=[
                            {
                                "type": "tool_call_chunk",
                                "name": TOOL_NAME,
                                "args": "{}",
                                "id": call_id,
                                "index": tool_index,
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
            async for piece in self._stream_text(answer):
                yield piece
            yield self._final_chunk("end_turn")
        except asyncio.CancelledError:
            self.probe["cancelled"] = True
            raise

    async def _stream_text(self, answer: str) -> AsyncIterator[ChatGenerationChunk]:
        for word in re.findall(r"\S+\s*", answer):
            await asyncio.sleep(self.delta_delay_ms / 1000)
            yield ChatGenerationChunk(
                message=AIMessageChunk(content=[{"type": "text", "text": word, "index": 1}])
            )

    def _remittance_step(
        self, messages: list[BaseMessage], tool_results: list[ToolMessage]
    ) -> tuple[str, Any] | None:
        """("tool", (name, args)) or ("text", answer) for a remittance turn, else None."""
        if not self.tools_bound or QUOTE_REMITTANCE not in self.bound_tool_names:
            return None
        texts = [_content_text(m) for m in messages if isinstance(m, HumanMessage)]
        if not any(_REMIT_REQUEST.search(text) for text in texts):
            return None
        results = {m.name: str(m.content) for m in tool_results}
        if QUOTE_REMITTANCE in results:
            return ("text", _quote_answer(results[QUOTE_REMITTANCE]))
        if GET_REMITTANCE_RECIPIENTS not in results:
            return ("tool", (GET_REMITTANCE_RECIPIENTS, {}))
        try:
            recipients = json.loads(results[GET_REMITTANCE_RECIPIENTS])["recipients"]
        except (ValueError, KeyError, TypeError):
            return ("text", "I could not load your saved recipients right now. Please try again.")
        slots = _remittance_slots(texts)
        if slots["recipient"] is None:
            names = " or ".join(f"{r['nameEn']} ({r['bankName']})" for r in recipients)
            return ("text", f"Who should receive the money: {names}?")
        if slots["amount"] is None:
            return ("text", "How much would you like to send?")
        if slots["currency"] is None:
            amount = f"{slots['amount']:,}"
            return ("text", f"Is the {amount} amount in KRW you send or PHP they receive?")
        recipient = next((r for r in recipients if r["relationship"] == slots["recipient"]), None)
        if recipient is None:
            return ("text", "That recipient is not saved. Please add the recipient in the app.")
        args = {
            "recipientId": recipient["recipientId"],
            "amount": slots["amount"],
            "currency": slots["currency"],
        }
        return ("tool", (QUOTE_REMITTANCE, args))

    def _product_step(
        self, question: str, tool_results: list[ToolMessage]
    ) -> tuple[str, Any] | None:
        """("tool", ...) or ("text", ...) for a product question when the product tool is bound."""
        if not self.tools_bound or RECOMMEND_SAVINGS_PRODUCTS not in self.bound_tool_names:
            return None
        if not _PRODUCT_REQUEST.search(question) or _NOT_PRODUCT.search(question):
            return None
        results = {m.name: str(m.content) for m in tool_results}
        if RECOMMEND_SAVINGS_PRODUCTS in results:
            return ("text", _product_answer(results[RECOMMEND_SAVINGS_PRODUCTS]))
        return ("tool", (RECOMMEND_SAVINGS_PRODUCTS, _product_args(question)))

    @staticmethod
    def _final_chunk(stop_reason: str) -> ChatGenerationChunk:
        return ChatGenerationChunk(
            message=AIMessageChunk(
                content=[],
                usage_metadata={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                response_metadata={"stop_reason": stop_reason, "model_name": FAKE_MODEL_ID},
            )
        )

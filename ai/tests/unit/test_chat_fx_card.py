"""환율 진단 카드(FX_SUMMARY_CARD, #95). Spring·LLM 없이 fixture와 가짜 모델로 돈다.

계약: specs/chat/fx_summary_tool_v1.md "화면 카드" 절. 필드 이름은 환율 화면 API(FX-001)와 같다.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.messages import AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.messages.tool import ToolCallChunk, tool_call_chunk
from langchain_core.outputs import ChatGenerationChunk

from app.chat.fake_llm import FAKE_MODEL_ID, FakeChatModel
from app.chat.fx_tools import (
    FIXTURE,
    FX_RATE_SUMMARY,
    FX_SUMMARY_CARD,
    FixtureFxSummary,
    FxSummaryCardBlock,
    FxTools,
    merge_fx_card,
    summary_block,
    summary_for_llm,
)
from app.chat.schemas import ChatStreamRequest, LlmConfig
from app.chat.stream import ChatRunOptions, run_chat
from app.chat.tools import ChatToolset, ToolOutcome

pytestmark = pytest.mark.unit

# fixture(tests/fixtures/chat/fx_summary_reyes.json)로 만든 카드의 통화 두 개.
USD_CARD: dict[str, Any] = {
    "currency": "USD",
    "unit": 1,
    "rate": 1406.51,
    "sendRate": 1420.5751,
    "receiveRate": 1392.4449,
    "change": -1.08,
    "changeRate": -0.08,
    "source": "KOREAEXIM",
    "asOf": "2026-11-06",
    "fetchedAt": "2026-11-06T11:05:00+09:00",
    "periodAverages": [
        {"period": "1M", "avg": 1407.7735, "diff": -1.2635, "diffRate": -0.09,
         "direction": "SAME", "businessDays": 23},
        {"period": "3M", "avg": 1429.6566, "diff": -23.1466, "diffRate": -1.62,
         "direction": "LOWER", "businessDays": 62},
        {"period": "6M", "avg": 1419.9966, "diff": -13.4866, "diffRate": -0.95,
         "direction": "LOWER", "businessDays": 126},
        {"period": "1Y", "avg": 1418.8393, "diff": -12.3293, "diffRate": -0.87,
         "direction": "LOWER", "businessDays": 246},
    ],
    "isMock": True,
    "stale": True,
}  # fmt: skip
PHP_CARD: dict[str, Any] = {
    "currency": "PHP",
    "unit": 1,
    "rate": 20.984,
    "sendRate": None,
    "receiveRate": None,
    "change": 0.112,
    "changeRate": 0.54,
    "source": "MARKET_REF",
    "asOf": "2026-11-17",
    "fetchedAt": "2026-11-18T14:13:52+09:00",
    "periodAverages": [
        {"period": "1M", "avg": 21.0393, "diff": -0.0553, "diffRate": -0.26,
         "direction": "LOWER", "businessDays": 22},
        {"period": "3M", "avg": 21.5431, "diff": -0.5591, "diffRate": -2.6,
         "direction": "LOWER", "businessDays": 67},
        {"period": "6M", "avg": 22.9364, "diff": -1.9524, "diffRate": -8.51,
         "direction": "LOWER", "businessDays": 132},
        {"period": "1Y", "avg": 23.8157, "diff": -2.8317, "diffRate": -11.89,
         "direction": "LOWER", "businessDays": 255},
    ],
    "isMock": False,
    "stale": False,
}  # fmt: skip
# 카드에 들어가면 안 되는 것: 문장·버튼 경로·LLM용 이름·FX-001에서 뺀 필드.
NOT_IN_CARD = {
    "quoteCurrency", "sourceLabel", "notices", "rateAt", "lastCheckedAt", "changePct",
    "changeFromPreviousBusinessDay", "insufficientDataPeriods", "detailPath", "summary", "text",
}  # fmt: skip


def _body() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def _tools(source: Any = None) -> FxTools:
    return FxTools(source or FixtureFxSummary(), user_id=1, request_id="req_c", timeout=5)


def _run(arguments: dict[str, Any], source: Any = None) -> ToolOutcome:
    return asyncio.run(_tools(source).run(FX_RATE_SUMMARY, arguments))


class _Fixed:
    """정해 둔 Spring 본문을 그대로 돌려주는 원천."""

    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body

    async def summary(self, user_id: int, currency: str | None, request_id: str | None) -> Any:
        return self.body


def _keys(card: dict[str, Any]) -> set[str]:
    keys = set(card)
    for item in card["currencies"]:
        keys |= set(item)
        for period in item["periodAverages"]:
            keys |= set(period)
    return keys


# --- 계약: 카드 하나 = 도구 결과(Spring 원본) ---------------------------------------------


def test_success_card_has_usd_and_php_from_spring_values() -> None:
    outcome = _run({})
    assert outcome.status == "SUCCEEDED"
    (card,) = outcome.blocks
    assert card == {
        "type": FX_SUMMARY_CARD,
        "currencies": [USD_CARD, PHP_CARD],
        "unavailableCurrencies": [],
    }
    # Contract 모델로 다시 읽어도 같은 값(형식 계약).
    assert FxSummaryCardBlock.model_validate(card).model_dump(mode="json", by_alias=True) == card
    assert not (_keys(card) & NOT_IN_CARD)
    # LLM에 주는 값은 카드와 상관없이 그대로(송금 환율 없음).
    content = json.loads(outcome.content)
    assert content == summary_for_llm(_body())
    assert "sendRate" not in json.dumps(content)


def test_single_currency_call_gives_a_card_with_that_currency() -> None:
    (card,) = _run({"currency": "PHP"}).blocks
    assert card == {"type": FX_SUMMARY_CARD, "currencies": [PHP_CARD], "unavailableCurrencies": []}
    # MARKET_REF는 송금 환율이 없다.
    assert (PHP_CARD["sendRate"], PHP_CARD["receiveRate"]) == (None, None)


@pytest.mark.parametrize(
    ("arguments", "status"),
    [
        ({"currency": "JPY"}, "REJECTED"),  # 화면 통화가 아님
        ({"currency": "EUR1"}, "REJECTED"),  # 인자 형식 오류
    ],
)
def test_no_card_unless_succeeded(arguments: dict[str, Any], status: str) -> None:
    outcome = _run(arguments)
    assert (outcome.status, outcome.blocks) == (status, [])


def test_no_card_when_rate_is_missing() -> None:
    body = {**_body(), "currencies": [c for c in _body()["currencies"] if c["currency"] == "USD"]}
    source = FixtureFxSummary()
    source._data = body  # PHP 실데이터 없음 → FX_RATE_NOT_FOUND
    outcome = _run({"currency": "PHP"}, source)
    assert (outcome.status, outcome.blocks) == ("FAILED", [])


def test_unavailable_currencies_are_listed() -> None:
    php_only = [c for c in _body()["currencies"] if c["currency"] == "PHP"]
    body = {**_body(), "currencies": php_only, "unavailableCurrencies": ["USD"]}
    (card,) = _run({}, _Fixed(body)).blocks
    assert [c["currency"] for c in card["currencies"]] == ["PHP"]
    assert card["unavailableCurrencies"] == ["USD"]


def test_insufficient_data_periods_are_left_out() -> None:
    php = next(c for c in _body()["currencies"] if c["currency"] == "PHP")
    short = {
        "avg": None,
        "diff": None,
        "diffRate": None,
        "direction": None,
        "businessDays": 40,
        "reason": "INSUFFICIENT_DATA",
    }
    periods = [php["periodAverages"][0], *({**short, "period": p} for p in ("3M", "6M", "1Y"))]
    body = {**_body(), "currencies": [{**php, "periodAverages": periods}]}
    (card,) = _run({}, _Fixed(body)).blocks
    [item] = card["currencies"]
    assert item["periodAverages"] == [PHP_CARD["periodAverages"][0]]
    # 평균이 하나도 없어도 카드는 낸다(현재 값은 있다).
    none = {**_body(), "currencies": [{**php, "periodAverages": [{**short, "period": "1M"}]}]}
    (card,) = _run({}, _Fixed(none)).blocks
    assert card["currencies"][0]["periodAverages"] == []


def test_no_previous_business_day_gives_null_change() -> None:
    php = next(c for c in _body()["currencies"] if c["currency"] == "PHP")
    body = {**_body(), "currencies": [{**php, "change": None, "changeRate": None}]}
    (card,) = _run({}, _Fixed(body)).blocks
    assert (card["currencies"][0]["change"], card["currencies"][0]["changeRate"]) == (None, None)


def test_empty_currency_list_gives_no_card() -> None:
    assert summary_block({"currencies": [], "unavailableCurrencies": ["USD", "PHP"]}) is None


def test_bad_card_values_skip_only_the_card() -> None:
    # 카드 형식만 어긋나면(환율 0) LLM 결과는 그대로 SUCCEEDED, 카드만 없다.
    php = next(c for c in _body()["currencies"] if c["currency"] == "PHP")
    body = {**_body(), "currencies": [{**php, "sendRate": 0}]}
    outcome = _run({}, _Fixed(body))
    assert (outcome.status, outcome.blocks) == ("SUCCEEDED", [])
    assert json.loads(outcome.content) == summary_for_llm(body)


# --- 한 턴에 여러 번: 카드는 하나(통화 기준 병합) ------------------------------------------


def _card(*items: dict[str, Any], unavailable: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": FX_SUMMARY_CARD,
        "currencies": list(items),
        "unavailableCurrencies": unavailable or [],
    }


def test_merge_two_single_currency_cards() -> None:
    blocks: list[dict[str, Any]] = []
    merge_fx_card(blocks, _card(USD_CARD))
    merge_fx_card(blocks, _card(PHP_CARD))
    assert blocks == [_card(USD_CARD, PHP_CARD)]


def test_merge_all_then_one_currency_keeps_one_entry_per_currency() -> None:
    newer = {**PHP_CARD, "fetchedAt": "2026-11-18T14:20:00+09:00"}
    blocks: list[dict[str, Any]] = []
    merge_fx_card(blocks, _card(USD_CARD, PHP_CARD))
    merge_fx_card(blocks, _card(newer))
    # 순서는 처음 나온 순서, 같은 통화는 나중 값.
    assert blocks == [_card(USD_CARD, newer)]


def test_merge_drops_unavailable_currencies_that_are_on_the_card() -> None:
    blocks: list[dict[str, Any]] = []
    merge_fx_card(blocks, _card(USD_CARD, unavailable=["PHP", "VND"]))
    merge_fx_card(blocks, _card(PHP_CARD))
    assert blocks == [_card(USD_CARD, PHP_CARD, unavailable=["VND"])]


def test_merge_keeps_other_blocks_and_the_first_card_position() -> None:
    other = {"type": "REMITTANCE_CONFIRM", "transferPath": "/transfer"}
    blocks: list[dict[str, Any]] = [_card(USD_CARD), other]
    merge_fx_card(blocks, _card(PHP_CARD))
    assert blocks == [_card(USD_CARD, PHP_CARD), other]


# --- 그래프·스트림까지: 두 번 불러도 agent.done blocks에 카드 하나 ---------------------------


def _tool_use(
    call_id: str, args: dict[str, Any], index: int
) -> tuple[dict[str, Any], ToolCallChunk]:
    use = {
        "type": "tool_use",
        "id": call_id,
        "name": FX_RATE_SUMMARY,
        "input": args,
        "index": index,
    }
    chunk = tool_call_chunk(name=FX_RATE_SUMMARY, args=json.dumps(args), id=call_id, index=index)
    return use, chunk


class FxCallsModel(FakeChatModel):
    """``steps``의 응답마다 fx_rate_summary를 그 인자들로 부르고, 다 부르면 짧게 답한다.

    ``[[USD, PHP]]``는 한 응답에 두 번, ``[[USD], [PHP]]``는 두 응답에 한 번씩.
    """

    steps: list[list[dict[str, Any]]] = []

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        answered = sum(isinstance(m, ToolMessage) for m in messages)
        step = next(
            (i for i in range(len(self.steps)) if sum(map(len, self.steps[:i])) == answered),
            None,
        )
        if step is not None:
            calls = self.steps[step]
            parts = [_tool_use(f"toolu_{step}_{i}", args, i + 1) for i, args in enumerate(calls)]
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content=[p[0] for p in parts], tool_call_chunks=[p[1] for p in parts]
                )
            )
            yield self._final_chunk("tool_use")
            return
        text = "Here are your exchange rates."
        yield ChatGenerationChunk(
            message=AIMessageChunk(content=[{"type": "text", "text": text, "index": 1}])
        )
        yield self._final_chunk("end_turn")


def _done_blocks(model: FakeChatModel) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    body = {"runId": "run_f", "userId": 1, "sessionId": 1, "language": "en", "message": "fx"}
    events: list[tuple[str, dict[str, Any]]] = []

    async def run() -> None:
        async for event in run_chat(
            ChatStreamRequest.model_validate(body),
            config=LlmConfig(model="m", thinking="model_default", effort="", max_tokens=100),
            model=model,
            tool=ChatToolset(_tools()),
            options=ChatRunOptions(3, 30, False, FAKE_MODEL_ID),
        ):
            assert event.raw_data is not None
            events.append((str(event.event), json.loads(event.raw_data)))

    asyncio.run(run())
    calls = [data for name, data in events if name == "agent.tool_call"]
    blocks: list[dict[str, Any]] = dict(events)["agent.done"]["blocks"]
    return calls, blocks


USD: dict[str, Any] = {"currency": "USD"}
PHP: dict[str, Any] = {"currency": "PHP"}
ALL: dict[str, Any] = {}  # currency 생략 = 화면 통화 전부


def test_two_calls_in_one_response_give_one_card() -> None:
    calls, blocks = _done_blocks(FxCallsModel(delta_delay_ms=0, steps=[[USD, PHP]]))
    assert [(c["arguments"], c["status"]) for c in calls] == [
        (USD, "SUCCEEDED"),
        (PHP, "SUCCEEDED"),
    ]
    assert blocks == [_card(USD_CARD, PHP_CARD)]


@pytest.mark.parametrize("steps", [[[USD], [PHP]], [[ALL], [PHP]], [[PHP], [ALL]]])
def test_calls_in_separate_responses_give_one_card(steps: list[list[dict[str, Any]]]) -> None:
    calls, blocks = _done_blocks(FxCallsModel(delta_delay_ms=0, steps=steps))
    assert len(calls) == 2
    (card,) = blocks
    # 통화마다 한 번, 처음 나온 순서.
    first = steps[0][0].get("currency")
    order = ["PHP", "USD"] if first == "PHP" else ["USD", "PHP"]
    assert [c["currency"] for c in card["currencies"]] == order
    assert card["unavailableCurrencies"] == []


def test_success_then_failure_keeps_only_the_successful_currency() -> None:
    steps = [[USD], [{"currency": "JPY"}]]
    calls, blocks = _done_blocks(FxCallsModel(delta_delay_ms=0, steps=steps))
    assert [c["status"] for c in calls] == ["SUCCEEDED", "REJECTED"]
    assert blocks == [_card(USD_CARD)]

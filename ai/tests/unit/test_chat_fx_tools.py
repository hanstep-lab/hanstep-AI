"""챗봇 환율 진단 도구(app.chat.fx_tools, #91). Spring·LLM 없이 fixture와 가짜 HTTP로 돈다."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from app.chat.fx_tools import (
    FIXTURE,
    FX_RATE_SUMMARY,
    NO_DATA,
    NOT_USER_CURRENCY,
    FixtureFxSummary,
    FxTools,
    SpringFxSummary,
    summary_for_llm,
)
from app.chat.number_check import check_answer
from app.chat.tools import UNAVAILABLE, ToolOutcome
from app.integrations.spring_client import SpringInternalClient

pytestmark = pytest.mark.unit

TOKEN = "s" * 40
# Reyes 로컬 실제 출력(tests/fixtures/chat/fx_summary_reyes.json)으로 LLM에 주는 값.
PHP_FOR_LLM: dict[str, Any] = {
    "currency": "PHP",
    "unit": 1,
    "quoteCurrency": "KRW",
    "rate": 20.984,
    "source": "MARKET_REF",
    "asOf": "2026-11-17",
    "lastCheckedAt": "2026-11-18T14:13:52+09:00",
    "changeFromPreviousBusinessDay": 0.112,
    "changePct": 0.54,
    "periodAverages": [
        {
            "period": "1M",
            "months": 1,
            "average": 21.0393,
            "difference": -0.0553,
            "differencePct": -0.26,
            "currentVsAverage": "LOWER",
            "businessDays": 22,
        },
        {
            "period": "3M",
            "months": 3,
            "average": 21.5431,
            "difference": -0.5591,
            "differencePct": -2.6,
            "currentVsAverage": "LOWER",
            "businessDays": 67,
        },
        {
            "period": "6M",
            "months": 6,
            "average": 22.9364,
            "difference": -1.9524,
            "differencePct": -8.51,
            "currentVsAverage": "LOWER",
            "businessDays": 132,
        },
        {
            "period": "1Y",
            "months": 12,
            "average": 23.8157,
            "difference": -2.8317,
            "differencePct": -11.89,
            "currentVsAverage": "LOWER",
            "businessDays": 255,
        },
    ],
    "insufficientDataPeriods": [],
    "isMock": False,
    "stale": False,
}


def _run(tools: FxTools, arguments: dict[str, Any]) -> ToolOutcome:
    return asyncio.run(tools.run(FX_RATE_SUMMARY, arguments))


def _fixture_tools() -> FxTools:
    return FxTools(FixtureFxSummary(), user_id=1, request_id="req_f", timeout=5)


def _spring_tools(
    handler: Any, *, user_id: int = 7, timeout: float = 5
) -> tuple[FxTools, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        response: httpx.Response = handler(request)
        return response

    http = httpx.Client(transport=httpx.MockTransport(record), base_url="http://spring")
    client = SpringInternalClient(base_url="http://spring", api_key=TOKEN, client=http)
    source = SpringFxSummary(client, timeout)
    return FxTools(source, user_id=user_id, request_id="req_s", timeout=timeout), seen


def _fixture_body() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def test_schema_has_only_an_optional_currency() -> None:
    [schema] = _fixture_tools().schemas
    assert schema["name"] == FX_RATE_SUMMARY
    assert schema["input_schema"]["properties"].keys() == {"currency"}
    assert "required" not in schema["input_schema"]
    assert schema["input_schema"]["additionalProperties"] is False


def test_all_currencies_from_fixture_values_only() -> None:
    outcome = _run(_fixture_tools(), {})
    assert outcome.status == "SUCCEEDED"
    content = json.loads(outcome.content)
    usd, php = content["currencies"]
    assert php == PHP_FOR_LLM
    # USD는 로컬 Mock 시드(11/06까지)라 isMock·stale이 그대로 간다. 송금 환율·문장은 없다.
    assert (usd["currency"], usd["source"], usd["isMock"], usd["stale"]) == (
        "USD",
        "KOREAEXIM",
        True,
        True,
    )
    assert content["unavailableCurrencies"] == []
    assert "sendRate" not in usd and "sourceLabel" not in usd
    # 측정 값(agent.metrics)에는 LLM에 준 값이 그대로 남는다.
    assert outcome.measurement == {"arguments": {}, "content": content}
    assert outcome.result_ref is None and outcome.blocks == []


def test_currency_argument_is_upper_cased() -> None:
    outcome = _run(_fixture_tools(), {"currency": " php "})
    assert outcome.status == "SUCCEEDED"
    assert json.loads(outcome.content) == {"currencies": [PHP_FOR_LLM], "unavailableCurrencies": []}


@pytest.mark.parametrize(
    "arguments",
    [{"userId": 2}, {"currency": "PESO"}, {"currency": 1}],
)
def test_other_arguments_are_rejected(arguments: dict[str, Any]) -> None:
    # 사용자 id는 LLM 인자로 줄 수 없다(요청마다 고정).
    outcome = _run(_fixture_tools(), arguments)
    assert (outcome.status, outcome.error_code) == ("REJECTED", "INVALID_ARGUMENTS")


def test_insufficient_periods_are_listed_without_numbers() -> None:
    body = {
        "userId": 1,
        "screenCurrencies": ["USD", "VND"],
        "currencies": [
            {
                **_first_php(),
                "currency": "VND",
                "unit": 100,
                "periodAverages": [
                    {**_first_php()["periodAverages"][0]},
                    *[
                        {
                            "period": p,
                            "avg": None,
                            "diff": None,
                            "diffRate": None,
                            "direction": None,
                            "businessDays": 40,
                            "reason": "INSUFFICIENT_DATA",
                        }
                        for p in ("3M", "6M", "1Y")
                    ],
                ],
            }
        ],
        "unavailableCurrencies": ["USD"],
    }
    content = summary_for_llm(body)
    [vnd] = content["currencies"]
    assert vnd["unit"] == 100
    assert [p["period"] for p in vnd["periodAverages"]] == ["1M"]
    assert vnd["insufficientDataPeriods"] == ["3M", "6M", "1Y"]
    assert content["unavailableCurrencies"] == ["USD"]


def _first_php() -> dict[str, Any]:
    php: dict[str, Any] = next(c for c in _fixture_body()["currencies"] if c["currency"] == "PHP")
    return php


def test_spring_call_is_for_the_bound_user_only() -> None:
    body = _fixture_body()
    tools, seen = _spring_tools(lambda r: httpx.Response(200, json=body), user_id=7)

    assert _run(tools, {}).status == "SUCCEEDED"
    assert _run(tools, {"currency": "PHP"}).status == "SUCCEEDED"
    first, second = seen
    assert first.url.path == "/internal/v1/ai-context/users/7/fx-summary"
    assert "currency" not in first.url.params
    assert second.url.params["currency"] == "PHP"
    assert first.headers["X-Internal-Token"] == TOKEN
    assert first.headers["X-Request-Id"] == "req_s"


@pytest.mark.parametrize(
    ("status", "code", "result", "content"),
    [
        (404, "FX_RATE_NOT_FOUND", "FAILED", NO_DATA),
        (404, "USER_NOT_FOUND", "FAILED", UNAVAILABLE),
        (400, "VALIDATION_ERROR", "REJECTED", NOT_USER_CURRENCY),
        (500, "INTERNAL_ERROR", "FAILED", UNAVAILABLE),
        (401, "UNAUTHORIZED", "FAILED", UNAVAILABLE),
    ],
)
def test_spring_errors_become_cannot_check(
    status: int, code: str, result: str, content: str
) -> None:
    tools, _ = _spring_tools(lambda r: httpx.Response(status, json={"code": code, "message": "x"}))
    outcome = _run(tools, {"currency": "PHP"})
    assert (outcome.status, outcome.content) == (result, content)
    assert outcome.error_code == f"SPRING_HTTP_{status}"
    assert outcome.measurement is None


def test_timeout_and_bad_body_become_unavailable() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    timed_out = _run(_spring_tools(slow)[0], {})
    assert (timed_out.status, timed_out.content, timed_out.error_code) == (
        "TIMEOUT",
        UNAVAILABLE,
        "SPRING_TIMEOUT",
    )
    bad = _run(_spring_tools(lambda r: httpx.Response(200, json={"currencies": [{}]}))[0], {})
    assert (bad.status, bad.content, bad.error_code) == ("FAILED", UNAVAILABLE, "INVALID_RESPONSE")


def test_source_slower_than_the_budget_times_out() -> None:
    class Slow:
        async def summary(self, user_id: int, currency: str | None, request_id: str | None) -> Any:
            await asyncio.sleep(1)

    outcome = _run(FxTools(Slow(), user_id=1, request_id=None, timeout=0.01), {})
    assert (outcome.status, outcome.content) == ("TIMEOUT", UNAVAILABLE)


def test_fixture_currency_errors_match_spring() -> None:
    jpy = _run(_fixture_tools(), {"currency": "JPY"})
    assert (jpy.status, jpy.content, jpy.error_code) == (
        "REJECTED",
        NOT_USER_CURRENCY,
        "SPRING_HTTP_400",
    )


def test_unknown_tool_name_is_rejected() -> None:
    outcome = asyncio.run(_fixture_tools().run("get_fx_rate", {}))
    assert (outcome.status, outcome.error_code) == ("REJECTED", "UNKNOWN_TOOL")


QUESTION = "How is PHP compared to last month?"


@pytest.mark.parametrize(
    "answer",
    [
        # 도구 결과의 숫자만(부호는 말로: differencePct -2.6 → "2.6% lower")
        "1 PHP = 20.984 KRW as of 2026-11-17 (market reference rate). It is 0.26% lower than "
        "the 1-month average of 21.0393 KRW over 22 business days, and 2.6% lower than the "
        "3-month average of 21.5431 KRW over 67 business days.",
        "Compared with the 12-month average (23.8157 KRW, 255 business days), today's 20.984 KRW "
        "is 11.89% lower. It rose 0.112 KRW (0.54%) from the previous business day.",
        "Last checked at 14:13 on 2026-11-18.",
    ],
)
def test_number_check_accepts_tool_numbers(answer: str) -> None:
    content = json.loads(_run(_fixture_tools(), {"currency": "PHP"}).content)
    check = check_answer(answer, [], [QUESTION], fx_results=[content])
    assert check.unsupported == [] and check.ambiguous == [], check.to_json()


@pytest.mark.parametrize(
    ("answer", "flagged"),
    [
        ("1 PHP = 21 KRW.", "21 KRW"),  # 반올림한 환율
        ("It may rise by 4% next month.", "4%"),  # 도구에 없는 예측 숫자
        ("The 2-month average is 21.2 KRW.", "21.2 KRW"),
    ],
)
def test_number_check_flags_numbers_not_in_the_tool(answer: str, flagged: str) -> None:
    content = json.loads(_run(_fixture_tools(), {"currency": "PHP"}).content)
    assert flagged in check_answer(answer, [], [QUESTION], fx_results=[content]).unsupported

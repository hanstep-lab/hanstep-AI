"""챗봇 송금 조회 도구(app.chat.remittance_tools). Spring·LLM 없이 fixture와 가짜 HTTP로 돈다."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.messages import AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk

from app.chat.fake_llm import FAKE_MODEL_ID, FakeChatModel
from app.chat.remittance_tools import (
    CARD_NOTE,
    GET_FX_RATE,
    GET_REMITTANCE_RECIPIENTS,
    QUOTE_REMITTANCE,
    FixtureRemittanceSource,
    RemittanceTools,
    SpringRemittanceSource,
)
from app.chat.schemas import ChatStreamRequest, LlmConfig
from app.chat.stream import ChatRunOptions, run_chat
from app.chat.tools import UNAVAILABLE, ChatToolset, ToolOutcome
from app.integrations.spring_client import SpringInternalClient

pytestmark = pytest.mark.unit

TOKEN = "s" * 40
LORNA = {
    "recipientId": 11,
    "nameEn": "LORNA REYES",
    "relationship": "MOTHER",
    "countryCode": "PH",
    "currency": "PHP",
    "bankName": "BDO",
    "accountLast4": "5566",
}
MARK = {
    "recipientId": 12,
    "nameEn": "MARK ANTHONY REYES",
    "relationship": "SELF",
    "countryCode": "PH",
    "currency": "PHP",
    "bankName": "BPI",
    "accountLast4": "7788",
}
RATE = {
    "currency": "PHP",
    "unit": 1,
    "rate": 22.32,
    "sourceLabel": "ECB 기준환율 기반 참고값",
    "asOf": "2026-11-06",
    "isMock": True,
}
RATE_FOR_LLM = {k: v for k, v in RATE.items() if k != "isMock"}


def _run(tools: RemittanceTools, name: str, arguments: dict[str, Any]) -> ToolOutcome:
    return asyncio.run(tools.run(name, arguments))


def _fixture_tools() -> RemittanceTools:
    return RemittanceTools(FixtureRemittanceSource(), user_id=1, request_id="req_r", timeout=5)


def test_schemas_are_the_three_tools() -> None:
    names = [schema["name"] for schema in _fixture_tools().schemas]
    assert names == [GET_REMITTANCE_RECIPIENTS, GET_FX_RATE, QUOTE_REMITTANCE]


def test_recipients_from_fixture() -> None:
    outcome = _run(_fixture_tools(), GET_REMITTANCE_RECIPIENTS, {})
    assert outcome.status == "SUCCEEDED"
    assert json.loads(outcome.content) == {"recipients": [LORNA, MARK]}


def test_fx_rate_from_fixture_without_is_mock() -> None:
    outcome = _run(_fixture_tools(), GET_FX_RATE, {"currency": "PHP"})
    assert outcome.status == "SUCCEEDED"
    assert json.loads(outcome.content) == RATE_FOR_LLM
    missing = _run(_fixture_tools(), GET_FX_RATE, {"currency": "USD"})
    assert (missing.status, missing.error_code) == ("REJECTED", "SPRING_HTTP_404")
    assert missing.content == "no reference rate for this currency"


@pytest.mark.parametrize(
    ("arguments", "send", "receive"),
    [
        ({"recipientId": 11, "amount": 500_000, "currency": "KRW"}, 500_000, 22_401),
        ({"recipientId": 11, "amount": 20_000, "currency": "PHP"}, 446_400, 20_000),
        ({"recipientId": 12, "amount": 200_000, "currency": "KRW"}, 200_000, 8_960),
        ({"recipientId": 12, "amount": 5_000, "currency": "PHP"}, 111_600, 5_000),
    ],
)
def test_quote_from_fixture(arguments: dict[str, Any], send: int, receive: int) -> None:
    outcome = _run(_fixture_tools(), QUOTE_REMITTANCE, arguments)
    assert outcome.status == "SUCCEEDED"
    assert outcome.arguments == arguments
    content = json.loads(outcome.content)
    recipient = LORNA if arguments["recipientId"] == 11 else MARK
    assert content == {
        "recipient": recipient,
        "recipientId": arguments["recipientId"],
        "sendAmount": {"amount": send, "currency": "KRW"},
        "receiveAmount": {"amount": receive, "currency": "PHP"},
        "appliedRate": RATE_FOR_LLM,
        "note": CARD_NOTE,
    }
    # 확인 카드: 견적 결과 그대로, 환율은 isMock까지, 송금 화면 경로의 amount는 보낼 원화 금액.
    assert outcome.blocks == [
        {
            "type": "REMITTANCE_CONFIRM",
            "recipient": recipient,
            "sendAmount": {"amount": send, "currency": "KRW"},
            "receiveAmount": {"amount": receive, "currency": "PHP"},
            "appliedRate": RATE,
            "transferPath": f"/transfer?recipientId={arguments['recipientId']}&amount={send}",
        }
    ]


@pytest.mark.parametrize(
    ("arguments", "error_code"),
    [
        ({"recipientId": 99, "amount": 1000, "currency": "KRW"}, "UNKNOWN_RECIPIENT"),
        ({"recipientId": 11, "amount": 300, "currency": "USD"}, "UNSUPPORTED_CURRENCY"),
        ({"recipientId": 11, "amount": 0, "currency": "KRW"}, "INVALID_ARGUMENTS"),
        ({"recipientId": 11, "amount": 1000}, "INVALID_ARGUMENTS"),
        ({"recipientId": 11, "amount": 1000, "currency": "KRW", "userId": 2}, "INVALID_ARGUMENTS"),
    ],
)
def test_quote_rejections(arguments: dict[str, Any], error_code: str) -> None:
    outcome = _run(_fixture_tools(), QUOTE_REMITTANCE, arguments)
    assert (outcome.status, outcome.error_code) == ("REJECTED", error_code)
    assert outcome.blocks == []


def test_unknown_tool_is_rejected() -> None:
    outcome = _run(_fixture_tools(), "send_money", {})
    assert (outcome.status, outcome.error_code) == ("REJECTED", "UNKNOWN_TOOL")


# --- Spring 내부 API ------------------------------------------------------------------


def _spring_tools(
    handler: Any, *, user_id: int = 7, timeout: float = 5
) -> tuple[RemittanceTools, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        response: httpx.Response = handler(request)
        return response

    http = httpx.Client(transport=httpx.MockTransport(record), base_url="http://spring")
    client = SpringInternalClient(base_url="http://spring", api_key=TOKEN, client=http)
    source = SpringRemittanceSource(client, timeout)
    return RemittanceTools(source, user_id=user_id, request_id="req_s", timeout=timeout), seen


def _spring_ok(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/remittance-recipients"):
        return httpx.Response(200, json={"recipients": [LORNA, MARK]})
    if request.url.path.endswith("/fx-rates"):
        return httpx.Response(200, json=RATE)
    params = request.url.params
    return httpx.Response(
        200,
        json={
            "recipientId": int(params["recipientId"]),
            "sendAmount": {"amount": int(params["amount"]), "currency": "KRW"},
            "receiveAmount": {"amount": 22_401, "currency": "PHP"},
            "appliedRate": RATE,
        },
    )


def test_spring_calls_use_the_contract_paths() -> None:
    tools, seen = _spring_tools(_spring_ok)
    assert _run(tools, GET_REMITTANCE_RECIPIENTS, {}).status == "SUCCEEDED"
    assert _run(tools, GET_FX_RATE, {"currency": "PHP"}).status == "SUCCEEDED"
    quote = _run(tools, QUOTE_REMITTANCE, {"recipientId": 11, "amount": 500_000, "currency": "KRW"})
    assert quote.status == "SUCCEEDED"
    assert [(r.method, r.url.path, dict(r.url.params)) for r in seen] == [
        ("GET", "/internal/v1/ai-context/users/7/remittance-recipients", {}),
        ("GET", "/internal/v1/ai-context/fx-rates", {"currency": "PHP"}),
        # 견적 전에 받는 분이 그 사용자의 것인지 확인한다.
        ("GET", "/internal/v1/ai-context/users/7/remittance-recipients", {}),
        (
            "GET",
            "/internal/v1/ai-context/users/7/remittance-quote",
            {"recipientId": "11", "amount": "500000", "currency": "KRW"},
        ),
    ]
    assert all(r.headers["X-Internal-Token"] == TOKEN for r in seen)
    assert all(r.headers["X-Request-Id"] == "req_s" for r in seen)


def test_spring_recipient_not_found_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/remittance-quote"):
            return httpx.Response(404, json={"code": "RECIPIENT_NOT_FOUND", "message": "x"})
        return _spring_ok(request)

    tools, _ = _spring_tools(handler)
    outcome = _run(tools, QUOTE_REMITTANCE, {"recipientId": 11, "amount": 1000, "currency": "KRW"})
    assert (outcome.status, outcome.error_code) == ("REJECTED", "SPRING_HTTP_404")


@pytest.mark.parametrize(
    ("response", "status", "error_code"),
    [
        (httpx.Response(500, json={"code": "INTERNAL_ERROR"}), "FAILED", "SPRING_HTTP_500"),
        (
            httpx.Response(200, json={"recipients": [{"recipientId": 11}]}),
            "FAILED",
            "INVALID_RESPONSE",
        ),
        (httpx.Response(200, json=[1, 2]), "FAILED", "SPRING_HTTP_200"),
    ],  # fmt: skip
)
def test_spring_failures(response: httpx.Response, status: str, error_code: str) -> None:
    tools, _ = _spring_tools(lambda _: response)
    outcome = _run(tools, GET_REMITTANCE_RECIPIENTS, {})
    assert (outcome.status, outcome.error_code, outcome.content) == (
        status,
        error_code,
        UNAVAILABLE,
    )


def test_spring_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    tools, _ = _spring_tools(handler)
    outcome = _run(tools, GET_FX_RATE, {"currency": "PHP"})
    assert (outcome.status, outcome.error_code) == ("TIMEOUT", "SPRING_TIMEOUT")


def test_quote_that_does_not_match_the_request_is_not_used() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/remittance-quote"):
            body = {
                "recipientId": 12,  # 요청은 11
                "sendAmount": {"amount": 500_000, "currency": "KRW"},
                "receiveAmount": {"amount": 22_401, "currency": "PHP"},
                "appliedRate": RATE,
            }
            return httpx.Response(200, json=body)
        return _spring_ok(request)

    tools, _ = _spring_tools(handler)
    outcome = _run(
        tools, QUOTE_REMITTANCE, {"recipientId": 11, "amount": 500_000, "currency": "KRW"}
    )
    assert (outcome.status, outcome.error_code) == ("FAILED", "INVALID_RESPONSE")
    assert outcome.blocks == []


def test_only_a_successful_quote_has_a_card() -> None:
    tools = _fixture_tools()
    assert _run(tools, GET_REMITTANCE_RECIPIENTS, {}).blocks == []
    assert _run(tools, GET_FX_RATE, {"currency": "PHP"}).blocks == []


# --- 그래프·스트림까지: 카드가 agent.done의 blocks로 나간다 ---------------------------------


class QuoteModel(FakeChatModel):
    """첫 호출에 quote_remittance를 부르고, 도구 결과를 받으면 짧게 답하는 가짜 모델."""

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        if not any(isinstance(m, ToolMessage) for m in messages):
            args = {"recipientId": 11, "amount": 500_000, "currency": "KRW"}
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content=[
                        {"type": "tool_use", "id": "toolu_q", "name": QUOTE_REMITTANCE,
                         "input": args, "index": 1}
                    ],
                    tool_call_chunks=[
                        {"type": "tool_call_chunk", "name": QUOTE_REMITTANCE,
                         "args": json.dumps(args), "id": "toolu_q", "index": 1}
                    ],
                )
            )  # fmt: skip
            yield self._final_chunk("tool_use")
            return
        text = "Please check the confirmation card."
        yield ChatGenerationChunk(
            message=AIMessageChunk(content=[{"type": "text", "text": text, "index": 1}])
        )
        yield self._final_chunk("end_turn")


def test_stream_puts_the_card_in_done_blocks() -> None:
    body = {"runId": "run_r", "userId": 1, "sessionId": 1, "language": "en", "message": "send"}
    events: list[tuple[str, dict[str, Any]]] = []

    async def run() -> None:
        async for event in run_chat(
            ChatStreamRequest.model_validate(body),
            config=LlmConfig(model="m", thinking="model_default", effort="", max_tokens=100),
            model=QuoteModel(delta_delay_ms=0),
            tool=ChatToolset(_fixture_tools()),
            options=ChatRunOptions(3, 30, False, FAKE_MODEL_ID),
        ):
            assert event.raw_data is not None
            events.append((str(event.event), json.loads(event.raw_data)))

    asyncio.run(run())
    by_name = dict(events)
    assert [name for name, _ in events] == ["agent.tool_call", "agent.delta", "agent.done"]
    call = by_name["agent.tool_call"]
    assert (call["toolName"], call["status"], call["resultRef"]) == (
        QUOTE_REMITTANCE,
        "SUCCEEDED",
        None,
    )
    (card,) = by_name["agent.done"]["blocks"]
    assert card["type"] == "REMITTANCE_CONFIRM"
    assert (card["sendAmount"]["amount"], card["receiveAmount"]["amount"]) == (500_000, 22_401)
    assert card["transferPath"] == "/transfer?recipientId=11&amount=500000"

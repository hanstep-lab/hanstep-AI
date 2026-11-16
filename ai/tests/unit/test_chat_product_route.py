"""INT-05 + 상품 추천 도구(v4, #65): 같은 그래프·이벤트 계약, 측정 전용 값, fixture 프로필 헤더.

실제 Anthropic·Spring 호출 없음(fake 모델, fixture 프로필).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.chat.product_tools import RECOMMEND_SAVINGS_PRODUCTS
from app.core.config import Settings
from app.integrations.policy_gateway import UnconfiguredPolicyGateway
from app.main import AppState, create_app
from app.rag.embeddings import BgeM3Embedder
from tests.unit.test_chat_stream import STREAM, TOKEN, TOOL_EVENT_KEYS, parse_sse

pytestmark = pytest.mark.unit


def _client(**overrides: Any) -> TestClient:
    values: dict[str, Any] = {
        "ai_internal_api_token": TOKEN,
        "chat_fake_delta_delay_ms": 0,
        "chat_llm_mode": "fake",
        "chat_prompt_version": "v4",
        **overrides,
    }
    settings = Settings(_env_file=None, **values)  # type: ignore[call-arg]
    app = create_app()
    app.state.rag = AppState(
        settings=settings, gateway=UnconfiguredPolicyGateway(), embedder=BgeM3Embedder()
    )
    return TestClient(app)


def _events(client: TestClient, message: str, **headers: str) -> list[tuple[str, dict[str, Any]]]:
    body = {
        "runId": "run_" + "p" * 32,
        "userId": 1,
        "sessionId": 9,
        "language": "en",
        "history": [],
        "message": message,
    }
    response = client.post(STREAM, json=body, headers={"X-Internal-Token": TOKEN, **headers})
    assert response.status_code == 200, response.text
    return parse_sse(response.text)


def test_v4_product_question_uses_the_product_tool() -> None:
    events = _events(_client(), "Which savings account should I open?", **{"X-Chat-Metrics": "1"})
    names = [name for name, _ in events]
    assert names[0] == "agent.tool_call" and names[-1] == "agent.done"
    call = events[0][1]
    assert call["toolName"] == RECOMMEND_SAVINGS_PRODUCTS and call["status"] == "SUCCEEDED"
    assert set(call) == TOOL_EVENT_KEYS  # 측정 값은 계약 이벤트에 싣지 않는다
    sources = next(data for name, data in events if name == "agent.sources")["sources"]
    assert sources and all(s["type"] == "PRODUCT_DOCUMENT" for s in sources)
    metrics = next(data for name, data in events if name == "agent.metrics")
    tool = metrics["tools"][0]
    assert tool["toolName"] == RECOMMEND_SAVINGS_PRODUCTS
    assert tool["conditions"] == {"productType": "SAVINGS"}
    assert tool["recommended"] and tool["rankedBy"] == "catalog"
    done = events[-1][1]
    assert done["promptVersion"] == "chat-v4"
    assert "These products fit your conditions" in done["content"]


def test_metrics_tools_only_with_the_metrics_header() -> None:
    events = _events(_client(), "Which savings account should I open?")
    assert "agent.metrics" not in [name for name, _ in events]


def test_fixture_profile_header_only_with_override() -> None:
    message = "I want to save 300,000 won every month. Which savings product is good?"
    headers = {"X-Chat-Metrics": "1", "X-Chat-Product-Profile": "P4"}
    allowed = _events(_client(chat_allow_config_override=True), message, **headers)
    tool = next(d for n, d in allowed if n == "agent.metrics")["tools"][0]
    assert tool["abstainReason"] == "REQUEST_EXCEEDS_SURPLUS"
    ignored = _events(_client(), message, **headers)
    tool = next(d for n, d in ignored if n == "agent.metrics")["tools"][0]
    assert tool["abstainReason"] is None  # 기본 P1(여유자금 675,317)


def test_unknown_fixture_profile_is_a_validation_error() -> None:
    client = _client(chat_allow_config_override=True)
    response = client.post(
        STREAM,
        json={
            "runId": "run_x",
            "userId": 1,
            "sessionId": 1,
            "language": "en",
            "message": "savings?",
        },
        headers={"X-Internal-Token": TOKEN, "X-Chat-Product-Profile": "P9"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_v3_has_no_product_tool() -> None:
    events = _events(_client(chat_prompt_version="v3"), "Which savings account should I open?")
    assert all(
        d["toolName"] != RECOMMEND_SAVINGS_PRODUCTS for n, d in events if n == "agent.tool_call"
    )


def test_card_question_does_not_call_the_product_tool() -> None:
    events = _events(_client(), "Which credit card should I get?")
    assert all(
        d["toolName"] != RECOMMEND_SAVINGS_PRODUCTS for n, d in events if n == "agent.tool_call"
    )


def test_profile_values_header_only_with_override() -> None:
    message = "I want to save 300,000 won every month. Which savings product is good?"
    values = (
        '{"visaTypeCode":"E-9","stayExpiresOn":"2028-11-16","age":null,'
        '"asOf":"2026-11-16","spareKrw":100000}'
    )
    headers = {"X-Chat-Metrics": "1", "X-Chat-Product-Profile": values}
    allowed = _events(_client(chat_allow_config_override=True), message, **headers)
    tool = next(d for n, d in allowed if n == "agent.metrics")["tools"][0]
    assert tool["abstainReason"] == "REQUEST_EXCEEDS_SURPLUS"
    assert tool["content"]["user"]["monthlySpareKrw"] == 100000
    ignored = _events(_client(), message, **headers)
    tool = next(d for n, d in ignored if n == "agent.metrics")["tools"][0]
    assert tool["content"]["user"]["monthlySpareKrw"] == 675317  # 운영과 같은 기본(P1)


def test_invalid_profile_values_header_is_a_validation_error() -> None:
    client = _client(chat_allow_config_override=True)
    response = client.post(
        STREAM,
        json={
            "runId": "run_x",
            "userId": 1,
            "sessionId": 1,
            "language": "en",
            "message": "savings?",
        },
        headers={"X-Internal-Token": TOKEN, "X-Chat-Product-Profile": '{"age": -1}'},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION_ERROR"

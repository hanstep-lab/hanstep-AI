"""송금 재질의 시나리오(fake 모드, v3): 애매 → 되묻기 → 답 → 견적 → 카드, 명확 → 견적 → 카드.

INT-05 라우트를 그대로 지난다. Spring·LLM API 없이 fixture 받는 분·환율을 쓴다.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.chat.remittance_tools import GET_REMITTANCE_RECIPIENTS, QUOTE_REMITTANCE
from app.chat.tools import TOOL_NAME
from app.core.config import Settings
from app.integrations.policy_gateway import UnconfiguredPolicyGateway
from app.main import AppState, create_app
from app.rag.embeddings import BgeM3Embedder

pytestmark = pytest.mark.unit

TOKEN = "f" * 40


def _client(version: str = "v3") -> TestClient:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        ai_internal_api_token=TOKEN,
        chat_fake_delta_delay_ms=0,
        chat_prompt_version=version,
    )
    app = create_app()
    app.state.rag = AppState(
        settings=settings, gateway=UnconfiguredPolicyGateway(), embedder=BgeM3Embedder()
    )
    return TestClient(app)


def _turn(
    client: TestClient, message: str, history: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    body = {
        "runId": "run_" + "r" * 32,
        "userId": 1,
        "sessionId": 1,
        "language": "en",
        "history": history or [],
        "message": message,
    }
    response = client.post(
        "/internal/v1/agent/chat/stream", json=body, headers={"X-Internal-Token": TOKEN}
    )
    assert response.status_code == 200
    events: list[tuple[str, dict[str, Any]]] = []
    name = None
    for line in response.text.splitlines():
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:") and name:
            events.append((name, json.loads(line[5:].strip())))
    done = dict(events)["agent.done"]
    return {
        "tools": [d for n, d in events if n == "agent.tool_call"],
        "answer": done["content"],
        "blocks": done["blocks"],
        "promptVersion": done["promptVersion"],
    }


def _card(turn: dict[str, Any]) -> dict[str, Any]:
    (card,) = turn["blocks"]
    assert card["type"] == "REMITTANCE_CONFIRM"
    return card


def test_clear_request_quotes_and_shows_the_card() -> None:
    turn = _turn(_client(), "send 500k won to mama pls")
    assert [(t["toolName"], t["status"]) for t in turn["tools"]] == [
        (GET_REMITTANCE_RECIPIENTS, "SUCCEEDED"),
        (QUOTE_REMITTANCE, "SUCCEEDED"),
    ]
    assert turn["tools"][1]["arguments"] == {
        "recipientId": 11,
        "amount": 500_000,
        "currency": "KRW",
    }
    card = _card(turn)
    assert card["recipient"]["nameEn"] == "LORNA REYES"
    assert (card["sendAmount"], card["receiveAmount"]) == (
        {"amount": 500_000, "currency": "KRW"},
        {"amount": 22_401, "currency": "PHP"},
    )
    assert card["transferPath"] == "/transfer?recipientId=11&amount=500000"
    # 답변 숫자는 견적 결과와 같다.
    assert "22,401 PHP" in turn["answer"] and "500,000 KRW" in turn["answer"]
    assert turn["promptVersion"] == "chat-v3"


def test_ambiguous_recipient_asks_then_quotes_after_the_reply() -> None:
    client = _client()
    first = _turn(client, "send 500k won home")
    assert [t["toolName"] for t in first["tools"]] == [GET_REMITTANCE_RECIPIENTS]
    assert first["blocks"] is None
    assert "LORNA REYES (BDO) or MARK ANTHONY REYES (BPI)?" in first["answer"]

    history = [
        {"role": "USER", "content": "send 500k won home"},
        {"role": "ASSISTANT", "content": first["answer"]},
    ]
    second = _turn(client, "to mama", history)
    assert [t["toolName"] for t in second["tools"]] == [
        GET_REMITTANCE_RECIPIENTS,
        QUOTE_REMITTANCE,
    ]
    assert _card(second)["receiveAmount"] == {"amount": 22_401, "currency": "PHP"}


def test_ambiguous_currency_asks_then_quotes_the_receive_amount() -> None:
    client = _client()
    first = _turn(client, "send 20000 to mom")
    assert first["blocks"] is None
    assert first["answer"] == "Is the 20,000 amount in KRW you send or PHP they receive?"
    history = [
        {"role": "USER", "content": "send 20000 to mom"},
        {"role": "ASSISTANT", "content": first["answer"]},
    ]
    card = _card(_turn(client, "pesos", history))
    assert (card["sendAmount"], card["receiveAmount"]) == (
        {"amount": 446_400, "currency": "KRW"},
        {"amount": 20_000, "currency": "PHP"},
    )


def test_v2_keeps_the_finance_tool_only() -> None:
    # 기본값 v2에는 송금 도구가 붙지 않는다(이전 동작 그대로).
    turn = _turn(_client("v2"), "send 500k won to mama pls")
    assert [t["toolName"] for t in turn["tools"]] == [TOOL_NAME]
    assert turn["blocks"] is None
    assert turn["promptVersion"] == "chat-v2"


def test_v3_question_without_a_remittance_uses_the_finance_tool() -> None:
    turn = _turn(_client(), "How much spare money do I have?")
    assert [t["toolName"] for t in turn["tools"]] == [TOOL_NAME]
    assert turn["blocks"] is None

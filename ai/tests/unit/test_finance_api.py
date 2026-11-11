from __future__ import annotations

import copy
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.finance.schemas import MAX_TRANSACTIONS
from app.integrations.policy_gateway import UnconfiguredPolicyGateway
from app.main import AppState, create_app
from app.rag.embeddings import BgeM3Embedder
from tests.unit._finance_cases import load_scenarios

pytestmark = pytest.mark.unit

TOKEN = "f" * 40
ANALYZE = "/internal/v1/finance/analyze"
SCENARIOS = {s["id"]: s for s in load_scenarios()}


def _make_client() -> TestClient:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        ai_internal_api_token=TOKEN,  # type: ignore[arg-type]
    )
    app = create_app()
    # No lifespan: no MongoDB, no model download.
    app.state.rag = AppState(
        settings=settings, gateway=UnconfiguredPolicyGateway(), embedder=BgeM3Embedder()
    )
    return TestClient(app)


@pytest.fixture
def client() -> TestClient:
    return _make_client()


def _headers() -> dict[str, str]:
    return {"X-Internal-Token": TOKEN}


def _request(scenario_id: str) -> dict[str, Any]:
    return copy.deepcopy(SCENARIOS[scenario_id]["request"])


def _assert_error_envelope(body: dict[str, Any], code: str) -> None:
    assert set(body) == {"code", "message", "details", "requestId"}
    assert body["code"] == code
    assert isinstance(body["message"], str)
    assert isinstance(body["details"], dict)
    assert body["requestId"]


@pytest.mark.parametrize("headers", [{}, {"X-Internal-Token": "wrong"}])
def test_rejects_missing_or_wrong_token(client: TestClient, headers: dict[str, str]) -> None:
    response = client.post(ANALYZE, json=_request("S01"), headers=headers)
    assert response.status_code == 401
    _assert_error_envelope(response.json(), "UNAUTHORIZED")


def test_ok_response_is_camel_case(client: TestClient) -> None:
    response = client.post(ANALYZE, json=_request("S01"), headers=_headers())
    assert response.status_code == 200
    body = response.json()
    assert body == SCENARIOS["S01"]["expected"]
    assert set(body) == {
        "avgMonthlyIncome",
        "fixedLiving",
        "remittance",
        "variableExpense",
        "spare",
        "dataQuality",
        "excluded",
    }
    assert set(body["dataQuality"]) == {
        "observedMonthCount",
        "missingMonths",
        "partialMonths",
        "warnings",
    }


def test_no_full_month_returns_nulls(client: TestClient) -> None:
    response = client.post(ANALYZE, json=_request("S13"), headers=_headers())
    assert response.status_code == 200
    body = response.json()
    for key in ("avgMonthlyIncome", "fixedLiving", "remittance", "variableExpense", "spare"):
        assert key in body and body[key] is None
    assert body["dataQuality"]["warnings"] == ["NO_FULL_MONTH"]


def test_twelve_full_months_is_allowed(client: TestClient) -> None:
    body = _request("S01")
    body["periodFrom"], body["periodTo"] = "2025-11-01", "2026-10-31"
    response = client.post(ANALYZE, json=body, headers=_headers())
    assert response.status_code == 200
    assert response.json()["dataQuality"]["observedMonthCount"] == 12


def _period_reversed(body: dict[str, Any]) -> None:
    body["periodFrom"], body["periodTo"] = "2026-09-30", "2026-09-01"


def _period_over_12_months(body: dict[str, Any]) -> None:
    body["periodFrom"], body["periodTo"] = "2025-09-01", "2026-09-30"


def _date_out_of_period(body: dict[str, Any]) -> None:
    body["transactions"][0]["bookedDate"] = "2026-10-01"


def _amount_zero(body: dict[str, Any]) -> None:
    body["transactions"][0]["amount"] = 0


def _amount_negative(body: dict[str, Any]) -> None:
    body["transactions"][0]["amount"] = -1000


def _same_id_different_content(body: dict[str, Any]) -> None:
    body["transactions"] = _request("S17")["transactions"]


def _snake_case_field(body: dict[str, Any]) -> None:
    body["period_from"] = body.pop("periodFrom")


def _category_code_bad_format(body: dict[str, Any]) -> None:
    body["transactions"][0]["categoryCode"] = "food<script>"


def _too_many_transactions(body: dict[str, Any]) -> None:
    tx = body["transactions"][0]
    body["transactions"] = [{**tx, "id": i} for i in range(MAX_TRANSACTIONS + 1)]


@pytest.mark.parametrize(
    "mutate",
    [
        _period_reversed,
        _period_over_12_months,
        _date_out_of_period,
        _amount_zero,
        _amount_negative,
        _same_id_different_content,
        _snake_case_field,
        _category_code_bad_format,
        _too_many_transactions,
    ],
)
def test_validation_errors(client: TestClient, mutate: Any) -> None:
    body = _request("S01")
    mutate(body)
    response = client.post(ANALYZE, json=body, headers=_headers())
    assert response.status_code == 400
    payload = response.json()
    _assert_error_envelope(payload, "VALIDATION_ERROR")
    assert payload["details"]["fields"]

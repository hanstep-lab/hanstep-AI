"""E-7-4 계산 API(10단계) 단위 테스트. MongoDB·임베딩 모델·네트워크 없이 실행한다."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.integrations.policy_gateway import UnconfiguredPolicyGateway
from app.main import AppState, create_app
from app.rag.embeddings import BgeM3Embedder
from app.visa.e74.engine import assess_e74
from app.visa.e74.scenario import apply_scenario
from app.visa.e74.schemas import MAX_VISA_EVIDENCES, E74Scenario
from tests.unit._e74_builders import AS_OF, eligible_body, profile

pytestmark = pytest.mark.unit

TOKEN = "e" * 40
CALCULATE = "/internal/v1/visa/e74/calculate"


def _client(*, include_draft: bool = True) -> TestClient:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        ai_internal_api_token=TOKEN,  # type: ignore[arg-type]
        e74_include_draft_rules=include_draft,
    )
    app = create_app()
    # lifespan 없이 띄운다: MongoDB 연결과 모델 다운로드를 하지 않는다.
    app.state.rag = AppState(
        settings=settings, gateway=UnconfiguredPolicyGateway(), embedder=BgeM3Embedder()
    )
    return TestClient(app)


@pytest.fixture
def client() -> TestClient:
    return _client()


def _post(client: TestClient, body: dict[str, Any]) -> Any:
    return client.post(CALCULATE, json=body, headers={"X-Internal-Token": TOKEN})


def _request(scenario: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"asOfDate": AS_OF.isoformat(), "profile": eligible_body(), "scenario": scenario}


@pytest.mark.parametrize("headers", [{}, {"X-Internal-Token": "wrong"}])
def test_rejects_missing_or_wrong_token(client: TestClient, headers: dict[str, str]) -> None:
    response = client.post(CALCULATE, json=_request(), headers=headers)
    assert response.status_code == 401


def test_returns_engine_result_with_rule_status(client: TestClient) -> None:
    response = _post(client, _request())
    assert response.status_code == 200
    body = response.json()

    # 명세 3.6 응답 예시의 필드
    assert body["status"] == "ELIGIBLE"
    assert body["score"] == {"base": 205, "bonus": 100, "penalty": 0, "total": 305, "required": 200}
    assert body["scoreRequirementMet"] is True
    assert body["eligibilityConfirmed"] is True
    assert body["needsEvidence"] == []
    assert body["ruleVersion"] == "E74-2026-v1"
    # 요건별 판정과 저장·표시용 필드
    assert {i["category"] for i in body["items"]} >= {"REQUIREMENT", "EXCLUSION", "BONUS"}
    assert body["ruleStatus"] == "DRAFT"
    assert body["isScenario"] is False and body["scenario"] is None
    # 채팅 Tool과 같은 엔진 결과(키까지 같다)
    engine = assess_e74(profile(eligible_body()), AS_OF, include_draft=True)
    assert body["requestKey"] == engine.request_key


def test_draft_rules_are_off_by_default() -> None:
    body = _post(_client(include_draft=False), _request()).json()
    assert body["status"] == "NEEDS_REVIEW"
    assert body["ruleVersion"] is None and body["ruleStatus"] is None
    assert body["eligibilityConfirmed"] is False
    assert body["reviewNotes"]


def test_same_input_gives_same_key_regardless_of_row_order(client: TestClient) -> None:
    first = _post(client, _request()).json()["requestKey"]
    reordered = _request()
    reordered["profile"]["visaEvidences"].reverse()
    assert _post(client, reordered).json()["requestKey"] == first


def test_scenario_korean_level_is_recalculated(client: TestClient) -> None:
    base = _post(client, _request()).json()
    scenario = {"addEvidences": [{"evidenceType": "TOPIK", "levelValue": "4"}]}
    body = _post(client, _request(scenario)).json()

    assert body["score"]["total"] == base["score"]["total"] + 40
    assert body["isScenario"] is True
    assert body["scenario"]["addEvidences"][0]["evidenceType"] == "TOPIK"
    assert body["requestKey"] != base["requestKey"]


def test_scenario_income_replaces_that_year(client: TestClient) -> None:
    scenario = {
        "incomeRecords": [
            {"incomeYear": 2024, "annualAmount": 36_000_000},
            {"incomeYear": 2025, "annualAmount": 36_000_000},
        ]
    }
    body = _post(client, _request(scenario)).json()
    assert body["score"]["total"] == 305 + 15


def test_scenario_as_of_date_moves_the_calculation_date(client: TestClient) -> None:
    body = _post(client, _request({"asOfDate": "2026-12-01"})).json()
    assert body["asOfDate"] == "2026-12-01"
    assert body["isScenario"] is True


def test_scenario_key_differs_from_plain_calculation_on_same_date(client: TestClient) -> None:
    # 기준일만 옮긴 시나리오와, 그 날짜로 바로 계산한 기본 계산은 키가 달라야 한다.
    plain = _request()
    plain["asOfDate"] = "2026-12-01"
    moved = _post(client, _request({"asOfDate": "2026-12-01"})).json()
    assert _post(client, plain).json()["requestKey"] != moved["requestKey"]


def test_empty_scenario_is_rejected(client: TestClient) -> None:
    response = _post(client, _request({}))
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert any(f["field"].startswith("scenario") for f in body["details"]["fields"])


def test_scenario_that_breaks_profile_limits_is_rejected(client: TestClient) -> None:
    request = _request({"addEvidences": [{"evidenceType": "DRIVER_LICENSE"}]})
    request["profile"]["visaEvidences"] = [
        {"visaEvidenceId": i, "evidenceType": "NATIONAL_CERTIFICATE"}
        for i in range(1, MAX_VISA_EVIDENCES + 1)
    ]
    response = _post(client, request)
    assert response.status_code == 400
    body = response.json()
    assert body["message"] == "invalid scenario"
    assert body["details"]["fields"] == [{"field": "profile.visaEvidences", "reason": "too_long"}]


def test_validation_error_does_not_echo_values(client: TestClient) -> None:
    request = _request()
    request["profile"]["visaProfile"]["birthDate"] = "SECRET-not-a-date"
    response = _post(client, request)
    assert response.status_code == 400
    assert "SECRET" not in response.text


def test_apply_scenario_keeps_original_profile() -> None:
    original = profile(eligible_body())
    scenario = E74Scenario.model_validate(
        {
            "addEvidences": [{"evidenceType": "TOPIK", "levelValue": "4"}],
            "incomeRecords": [{"incomeYear": 2025, "annualAmount": 40_000_000}],
        }
    )
    as_of = date(2026, 11, 1)
    applied = apply_scenario(original, scenario, as_of)

    added = applied.visa_evidences[-1]
    assert added.visa_evidence_id == max(e.visa_evidence_id for e in original.visa_evidences) + 1
    assert (added.status.value, added.issued_on) == ("VERIFIED", as_of)
    by_year = {r.income_year: r for r in applied.income_records}
    assert by_year[2025].annual_amount == 40_000_000
    assert by_year[2025].source.value == "DOCUMENT"
    assert by_year[2024] == next(r for r in original.income_records if r.income_year == 2024)
    # 원래 프로필은 그대로
    assert len(original.visa_evidences) == 3
    assert {r.annual_amount for r in original.income_records} == {31_500_000, 33_800_000}

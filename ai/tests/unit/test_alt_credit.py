"""대안신용 모의 사전진단: 판정 순서, 모델 파일 검증, 내부 API. 네트워크·DB 없이 실행한다."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.credit.alt_score import (
    DISCLAIMER,
    MODEL_PATH,
    AltCreditDecision,
    AltCreditInput,
    AltCreditReason,
    LimitBinding,
    assess,
    default_model,
    load_model,
)
from app.credit.schemas import AltCreditRequest
from app.integrations.policy_gateway import UnconfiguredPolicyGateway
from app.main import AppState, create_app
from app.rag.embeddings import BgeM3Embedder

pytestmark = pytest.mark.unit

TOKEN = "c" * 40
ASSESS = "/internal/v1/credit/alt-assessments"

# Reyes(BE 데모 시드·Mock 거래, 2026-11-16 기준). 연체 0회·청약·대환 없음은 임시값이다.
REYES = AltCreditInput(
    stay_duration_months=56,
    months_to_expiry=10,
    monthly_income=2_350_000,
    work_permitted=True,
    salary_months=12,
    shinhan_payroll=True,
    has_subscription=False,
    is_refinance=False,
    tel_late_6m=0,
    util_late_6m=0,
    remit_ratio=700_000 / 2_350_000,
    remit_months_6m=6,
)


def test_reyes_example() -> None:
    out = assess(REYES, is_mock=True)
    assert out.model_version == "alt-credit-v9"
    assert out.is_mock is True
    assert out.score == 818
    assert out.decision is AltCreditDecision.ELIGIBLE and out.reason_code is None
    assert out.loan_term_months == 9  # 만료까지 10개월 − 1
    assert (out.min_limit, out.max_limit) == (6_200_000, 7_800_000)
    assert out.max_limit_binding is LimitBinding.SIMPLE_DSR_40
    assert out.rate_breakdown is not None
    assert out.rate_breakdown.preferential == pytest.approx(0.010)  # 신한 급여이체만
    assert out.rate_breakdown.final == pytest.approx(0.1101805340, abs=1e-9)
    assert out.rate_base_date == date(2026, 11, 11)
    assert (
        out.disclaimer == DISCLAIMER == "모의 사전진단이며 실제 대출 승인이나 신용점수가 아닙니다"
    )


def test_is_mock_comes_from_the_caller() -> None:
    assert assess(REYES, is_mock=False).is_mock is False


def test_no_remittance_uses_the_training_neutral() -> None:
    # 중립값을 5로 두면 "송금 안 함"은 5개월 송금한 사람과 같은 입력이 된다.
    model = replace(default_model(), remit_regularity_neutral=5.0)
    no_remit = assess(replace(REYES, remit_months_6m=None), is_mock=True, model=model)
    five = assess(replace(REYES, remit_months_6m=5), is_mock=True, model=model)
    assert no_remit.pd == five.pd
    assert default_model().remit_regularity_neutral == pytest.approx(4.8252, abs=1e-4)


@pytest.mark.parametrize(
    ("overrides", "decision", "reason"),
    [
        ({"monthly_income": 0}, AltCreditDecision.NOT_ELIGIBLE, AltCreditReason.NO_INCOME),
        (
            {"work_permitted": False},
            AltCreditDecision.NOT_ELIGIBLE,
            AltCreditReason.WORK_NOT_PERMITTED,
        ),
        ({"salary_months": 2}, AltCreditDecision.NOT_ELIGIBLE, AltCreditReason.SALARY_UNDER_3M),
        (
            {"months_to_expiry": 6},
            AltCreditDecision.CONDITION_NOT_MET,
            AltCreditReason.EXPIRY_WITHIN_6M,
        ),
        (
            {"tel_late_6m": 6, "util_late_6m": 6, "remit_ratio": 0.8, "remit_months_6m": 1},
            AltCreditDecision.CONDITION_NOT_MET,
            AltCreditReason.RATE_OVER_LEGAL_MAX,
        ),
        # 앞의 검사가 먼저다: 소득이 없으면 근로 자격·만료를 보지 않는다.
        (
            {"monthly_income": 0, "work_permitted": False, "months_to_expiry": 1},
            AltCreditDecision.NOT_ELIGIBLE,
            AltCreditReason.NO_INCOME,
        ),
    ],
)
def test_decision_and_reason(
    overrides: dict[str, Any], decision: AltCreditDecision, reason: AltCreditReason
) -> None:
    out = assess(replace(REYES, **overrides), is_mock=True)
    assert (out.decision, out.reason_code) == (decision, reason)
    assert (out.loan_term_months, out.min_limit, out.max_limit) == (0, 0, 0)
    assert out.max_limit_binding is None
    # 원본은 소득자에게만 금리를 낸다. 점수는 모두에게 낸다.
    assert (out.rate_breakdown is None) == (reason is AltCreditReason.NO_INCOME)
    assert 1 <= out.score <= 1000


def test_rate_over_legal_max_shows_the_rate() -> None:
    risky = replace(REYES, tel_late_6m=6, util_late_6m=6, remit_ratio=0.8, remit_months_6m=1)
    out = assess(risky, is_mock=True)
    assert out.rate_breakdown is not None and out.rate_breakdown.final > 0.20


def test_loan_term_ends_the_month_before_expiry() -> None:
    assert assess(replace(REYES, months_to_expiry=7), is_mock=True).loan_term_months == 6
    assert assess(replace(REYES, months_to_expiry=100), is_mock=True).loan_term_months == 36
    # 만료 없음(영주 등): 최장 36개월
    assert assess(replace(REYES, months_to_expiry=None), is_mock=True).loan_term_months == 36


def test_existing_debt_and_payment_lower_the_limits() -> None:
    base = assess(REYES, is_mock=True)
    debt = assess(replace(REYES, existing_debt=1_000_000), is_mock=True)
    payment = assess(replace(REYES, existing_monthly_payment=300_000), is_mock=True)
    assert debt.max_limit == base.max_limit - 1_000_000
    assert payment.max_limit < base.max_limit and payment.min_limit < base.min_limit
    # 월상환 여력이 없으면 한도 0
    assert assess(replace(REYES, existing_monthly_payment=940_000), is_mock=True).max_limit == 0


def test_preferential_rate_is_capped() -> None:
    out = assess(replace(REYES, has_subscription=True, is_refinance=True), is_mock=True)
    assert out.rate_breakdown is not None
    # 거래실적(급여이체 1.0 + 청약 0.5, 최고 1.5) + 대환 0.3
    assert out.rate_breakdown.preferential == pytest.approx(0.018)


# ---------------------------------------------------------------- 모델 파일 검증


def _write_model(tmp_path: Path, change: Any) -> Path:
    data = json.loads(MODEL_PATH.read_text(encoding="utf-8"))
    change(data)
    path = tmp_path / "model.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_model_file_loads() -> None:
    model = load_model()
    assert model.version == "alt-credit-v9"
    assert model.coef.shape == (5,)
    assert model.params.base_rate_date == date(2026, 11, 11)
    assert model.params.product_max == 30_000_000


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(features=list(reversed(d["features"]))),
        lambda d: d.update(coef=d["coef"][:4]),
        lambda d: d["assumptions"].pop("LGD"),
        lambda d: d["assumptions"]["LGD"].update(value=True),
        lambda d: d["assumptions"]["MAX_TERM"].update(value=36.5),
        lambda d: d["assumptions"]["USE_WELCOME"].update(value=1),
        lambda d: d.update(remit_regularity_neutral=0),
        lambda d: d.update(unknown=1),
    ],
    ids=[
        "feature-order",
        "coef-length",
        "missing-assumption",
        "bool-as-number",
        "float-as-int",
        "int-as-flag",
        "neutral-range",
        "extra-field",
    ],
)
def test_invalid_model_file_is_rejected(tmp_path: Path, change: Any) -> None:
    with pytest.raises(ValueError):
        load_model(_write_model(tmp_path, change))


# ---------------------------------------------------------------- 내부 API


def _client() -> TestClient:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        ai_internal_api_token=TOKEN,  # type: ignore[arg-type]
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


def _body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "isMock": True,
        "stayDurationMonths": 56,
        "monthsToExpiry": 10,
        "monthlyIncome": 2_350_000,
        "workPermitted": True,
        "salaryMonths": 12,
        "shinhanPayroll": True,
        "hasSubscription": False,
        "isRefinance": False,
        "telLate6m": 0,
        "utilLate6m": 0,
        "remitRatio": 700_000 / 2_350_000,
        "remitMonths6m": 6,
    }
    body.update(changes)
    return body


def _post(client: TestClient, body: dict[str, Any]) -> Any:
    return client.post(ASSESS, json=body, headers={"X-Internal-Token": TOKEN})


@pytest.mark.parametrize("headers", [{}, {"X-Internal-Token": "wrong"}])
def test_api_rejects_missing_or_wrong_token(client: TestClient, headers: dict[str, str]) -> None:
    assert client.post(ASSESS, json=_body(), headers=headers).status_code == 401


def test_api_returns_the_assessment(client: TestClient) -> None:
    response = _post(client, _body())
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "modelVersion",
        "isMock",
        "pd",
        "score",
        "rateBreakdown",
        "rateBaseDate",
        "loanTermMonths",
        "minLimit",
        "maxLimit",
        "maxLimitBinding",
        "decision",
        "reasonCode",
        "disclaimer",
    }
    assert set(body["rateBreakdown"]) == {
        "base",
        "creditPremium",
        "capitalCost",
        "otherSpread",
        "spread",
        "preferential",
        "final",
    }
    assert body == assess(REYES, is_mock=True).model_dump(mode="json", by_alias=True)
    assert body["rateBaseDate"] == "2026-11-11"
    assert body["decision"] == "ELIGIBLE" and body["reasonCode"] is None


def test_api_passes_optional_debt_and_nulls(client: TestClient) -> None:
    body = _body(monthsToExpiry=None, remitMonths6m=None, existingDebt=1_000_000)
    response = _post(client, body)
    assert response.status_code == 200
    expected = replace(REYES, months_to_expiry=None, remit_months_6m=None, existing_debt=1_000_000)
    assert response.json() == assess(expected, is_mock=True).model_dump(mode="json", by_alias=True)
    assert AltCreditRequest.model_validate(body).to_input() == expected


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({k: v for k, v in _body().items() if k != "monthsToExpiry"}, "monthsToExpiry"),
        ({k: v for k, v in _body().items() if k != "isMock"}, "isMock"),
        (_body(telLate6m=7), "telLate6m"),
        (_body(utilLate6m=-1), "utilLate6m"),
        (_body(remitMonths6m=0), "remitMonths6m"),
        (_body(remitRatio=1.5), "remitRatio"),
        (_body(monthlyIncome=-1), "monthlyIncome"),
        (_body(tel_late_6m=0), "tel_late_6m"),  # snake_case는 모르는 필드
        (_body(userId=185), "userId"),
    ],
)
def test_api_rejects_invalid_input(client: TestClient, body: dict[str, Any], field: str) -> None:
    response = _post(client, body)
    assert response.status_code == 400
    error = response.json()
    assert error["code"] == "VALIDATION_ERROR"
    assert field in {f["field"] for f in error["details"]["fields"]}

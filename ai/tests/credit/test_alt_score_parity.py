"""대안신용 V9 서빙 함수와 원본 결과의 일치 테스트.

픽스처(``tests/fixtures/alt_credit_v9_demo.json``)는 ``scripts/build_alt_credit_v9.py``가 원본 V9
함수로 만든 데모 1,000명(seed=42)의 입력과 결과다. 원본 데모는 비송금자의 송금 규칙성을
데모 모집단 평균으로 채우므로, 여기서는 서빙 모델의 중립값을 그 값으로 바꿔 비교한다.
sklearn·scipy 없이 돈다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

import pytest

from app.credit.alt_score import (
    MODEL_PATH,
    AltCreditAssessment,
    AltCreditDecision,
    AltCreditInput,
    AltCreditReason,
    LimitBinding,
    assess,
    default_model,
)
from tests.conftest import AI_ROOT, FIXTURES

pytestmark = pytest.mark.unit

FIXTURE = FIXTURES / "alt_credit_v9_demo.json"
SOURCE = AI_ROOT / "models" / "alt_credit" / "v9_source.py"
RATE_TOLERANCE = 1e-9
PD_TOLERANCE = 1e-12

# 원본 값 → 서빙 코드
DECISIONS = {
    "승인": AltCreditDecision.ELIGIBLE,
    "대상아님": AltCreditDecision.NOT_ELIGIBLE,
    "거절": AltCreditDecision.CONDITION_NOT_MET,
}
REASONS = {
    "": None,
    "소득 없음": AltCreditReason.NO_INCOME,
    "근로 가능 자격 미확인(시간제취업 허가 없음 등)": AltCreditReason.WORK_NOT_PERMITTED,
    "급여 입금 3개월 미만": AltCreditReason.SALARY_UNDER_3M,
    "체류만료 6개월 이내": AltCreditReason.EXPIRY_WITHIN_6M,
    "산출금리가 법정최고금리 초과": AltCreditReason.RATE_OVER_LEGAL_MAX,
}
BINDINGS = {
    "": None,
    "상품 최대(3천만)": LimitBinding.PRODUCT_MAX,
    "연소득×80%": LimitBinding.ANNUAL_INCOME_80,
    "월소득×40%×개월수": LimitBinding.MONTHLY_PAYMENT_40_TERM,
    "간이 DSR 40%": LimitBinding.SIMPLE_DSR_40,
}
RATE_FIELDS = {
    "rate_base": "base",
    "rate_credit_premium": "credit_premium",
    "rate_capital_cost": "capital_cost",
    "rate_other_spread": "other_spread",
    "rate_spread": "spread",
    "rate_preferential": "preferential",
    "rate": "final",
}


@pytest.fixture(scope="module")
def fixture() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def _rows(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(zip(fixture["columns"], row, strict=True)) for row in fixture["rows"]]


def _input(row: dict[str, Any]) -> AltCreditInput:
    expiry, months = row["months_to_expiry"], row["remit_months_6m"]  # 원본 NaN → null
    return AltCreditInput(
        stay_duration_months=row["stay_duration_months"],
        months_to_expiry=None if expiry is None else int(expiry),
        monthly_income=row["monthly_income"],
        work_permitted=bool(row["work_permitted"]),
        salary_months=row["salary_months"],
        shinhan_payroll=bool(row["shinhan_payroll"]),
        has_subscription=bool(row["has_subscription"]),
        is_refinance=bool(row["is_refinance"]),
        tel_late_6m=row["tel_late_6m"],
        util_late_6m=row["util_late_6m"],
        remit_ratio=row["remit_ratio"],
        remit_months_6m=None if months is None else int(months),
        existing_debt=row["existing_debt"],
        existing_monthly_payment=row["existing_monthly_payment"],
    )


def _differences(row: dict[str, Any], out: AltCreditAssessment) -> list[str]:
    exact: list[tuple[str, Any, Any]] = [
        ("score", int(row["credit_score"]), out.score),
        ("loan_term", row["loan_term"], out.loan_term_months),
        ("min_limit", row["min_limit"], out.min_limit),
        ("max_limit", row["max_limit"], out.max_limit),
        ("decision", DECISIONS[row["decision"]], out.decision),
        ("reason", REASONS[row["reason"]], out.reason_code),
        ("max_limit_binding", BINDINGS[row["max_limit_binding"]], out.max_limit_binding),
    ]
    found = [f"{name}: expected {want!r}, got {got!r}" for name, want, got in exact if want != got]
    if abs(out.pd - row["pd"]) > PD_TOLERANCE:
        found.append(f"pd: expected {row['pd']!r}, got {out.pd!r}")
    rate = out.rate_breakdown
    if rate is None:
        found.append("rate: expected a rate breakdown, got null")
        return found
    for column, field in RATE_FIELDS.items():
        got = getattr(rate, field)
        if abs(got - row[column]) > RATE_TOLERANCE:
            found.append(f"{column}: expected {row[column]!r}, got {got!r}")
    return found


def test_fixture_and_model_come_from_the_same_original(fixture: dict[str, Any]) -> None:
    model_file = json.loads(MODEL_PATH.read_text(encoding="utf-8"))
    source_sha256 = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    assert fixture["meta"]["source_sha256"] == model_file["source"]["sha256"] == source_sha256
    assert fixture["meta"]["model_version"] == default_model().version == "alt-credit-v9"
    assert (
        fixture["meta"]["remit_regularity_neutral_training"]
        == default_model().remit_regularity_neutral
    )


def test_serving_matches_original_for_every_earner(fixture: dict[str, Any]) -> None:
    model = replace(
        default_model(), remit_regularity_neutral=fixture["meta"]["remit_regularity_neutral_demo"]
    )
    earners = [row for row in _rows(fixture) if row["monthly_income"] > 0]
    # 비교 인원이 0명이면 통과로 보지 않는다.
    assert len(earners) == fixture["meta"]["earners"] > 0

    mismatches = [
        f"{row['user_id']} {difference}"
        for row in earners
        for difference in _differences(row, assess(_input(row), is_mock=True, model=model))
    ]
    assert not mismatches, f"{len(mismatches)} mismatches in {len(earners)} earners:\n" + "\n".join(
        mismatches
    )


def test_non_earners_get_no_income_without_rate(fixture: dict[str, Any]) -> None:
    model = replace(
        default_model(), remit_regularity_neutral=fixture["meta"]["remit_regularity_neutral_demo"]
    )
    non_earners = [row for row in _rows(fixture) if row["monthly_income"] <= 0]
    assert non_earners
    for row in non_earners:
        out = assess(_input(row), is_mock=True, model=model)
        assert (out.score, out.decision, out.reason_code) == (
            int(row["credit_score"]),
            AltCreditDecision.NOT_ELIGIBLE,
            AltCreditReason.NO_INCOME,
        ), row["user_id"]
        assert out.rate_breakdown is None and row["rate"] is None
        assert (out.loan_term_months, out.min_limit, out.max_limit) == (0, 0, 0)

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.visa.e74.base_score import (
    calculate_age_score,
    calculate_income_score,
    calculate_korean_score,
    income_basis,
    target_income_years,
)
from app.visa.e74.dates import add_months, age_on, full_months_between
from app.visa.e74.results import ItemResult, ItemStatus
from app.visa.e74.rules import RuleSet, default_rule_sets, select_rule_set
from app.visa.e74.schemas import E74Profile

pytestmark = pytest.mark.unit

AS_OF = date(2026, 10, 25)


def _rules(as_of: date = AS_OF) -> RuleSet:
    rule_set = select_rule_set(as_of, default_rule_sets(), include_draft=True)
    assert rule_set is not None
    return rule_set


def _profile(
    *,
    birth_date: str | None = "1994-05-12",
    profile_status: str = "VERIFIED",
    incomes: list[dict[str, Any]] | None = None,
    evidences: list[dict[str, Any]] | None = None,
    industry: str | None = "MANUFACTURING",
) -> E74Profile:
    body: dict[str, Any] = {
        "visaProfile": {
            "visaTypeCode": "E-9",
            "birthDate": birth_date,
            "evidenceStatus": profile_status,
        },
        "incomeRecords": incomes or [],
        "visaEvidences": evidences or [],
    }
    if industry is not None:
        body["employmentHistories"] = [
            {
                "employmentHistoryId": 1,
                "employerName": "합성사업장",
                "industryCode": industry,
                "startedOn": "2021-01-04",
            }
        ]
    return E74Profile.model_validate(body)


def _income(
    year: int, amount: int, source: str = "DOCUMENT", status: str = "VERIFIED"
) -> dict[str, Any]:
    return {
        "incomeYear": year,
        "annualAmount": amount,
        "source": source,
        "verificationStatus": status,
    }


def _evidence(
    evidence_id: int, kind: str, status: str = "VERIFIED", **extra: Any
) -> dict[str, Any]:
    return {"visaEvidenceId": evidence_id, "evidenceType": kind, "status": status, **extra}


def _scores(result: ItemResult) -> tuple[ItemStatus, int | None, int | None]:
    return result.status, result.earned_points, result.potential_points


# --- 날짜 도우미 ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("birth", "as_of", "age"),
    [
        (date(2000, 10, 25), date(2026, 10, 25), 26),  # 생일 당일
        (date(2000, 10, 26), date(2026, 10, 25), 25),  # 생일 전날
        (date(2000, 2, 29), date(2026, 2, 28), 25),  # 평년 2월 28일은 아직 생일 전
        (date(2000, 2, 29), date(2026, 3, 1), 26),
    ],
)
def test_age_on(birth: date, as_of: date, age: int) -> None:
    assert age_on(birth, as_of) == age


def test_month_helpers() -> None:
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2025, 3, 10), 24) == date(2027, 3, 10)
    assert full_months_between(date(2021, 4, 5), date(2024, 4, 5)) == 36
    assert full_months_between(date(2021, 4, 5), date(2024, 4, 4)) == 35
    assert full_months_between(date(2024, 1, 1), date(2023, 1, 1)) == 0


# --- 나이 --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("birth", "rule_code", "points"),
    [
        ("2007-10-25", "AGE_19_26", 40),  # 만 19세가 되는 날
        ("1999-10-26", "AGE_19_26", 40),  # 26세 마지막 날
        ("1999-10-25", "AGE_27_33", 60),  # 27세 첫날
        ("1992-10-26", "AGE_27_33", 60),
        ("1992-10-25", "AGE_34_40", 30),
        ("1985-10-26", "AGE_34_40", 30),
        ("1985-10-25", "AGE_41_PLUS", 10),
    ],
)
def test_age_band_boundaries(birth: str, rule_code: str, points: int) -> None:
    result = calculate_age_score(_profile(birth_date=birth), _rules(), AS_OF)
    assert result.rule_code == rule_code
    assert _scores(result) == (ItemStatus.MET, points, points)


def test_under_19_needs_review() -> None:
    result = calculate_age_score(_profile(birth_date="2007-10-26"), _rules(), AS_OF)
    assert result.status is ItemStatus.NEEDS_REVIEW and result.evidence["age"] == 18


def test_missing_birth_date_is_asked_not_zeroed() -> None:
    result = calculate_age_score(_profile(birth_date=None), _rules(), AS_OF)
    assert _scores(result) == (ItemStatus.NEEDS_EVIDENCE, None, None)
    assert [m.field for m in result.missing_inputs] == ["visaProfile.birthDate"]


@pytest.mark.parametrize("status", ["DECLARED", "NEEDS_VERIFICATION", "PARTIALLY_VERIFIED"])
def test_unverified_birth_date_gives_potential_only(status: str) -> None:
    result = calculate_age_score(_profile(profile_status=status), _rules(), AS_OF)
    assert _scores(result) == (ItemStatus.NEEDS_EVIDENCE, 0, 60)


# --- 평균소득 ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("as_of", "years"),
    [
        (date(2026, 5, 31), (2023, 2024)),  # 전년도 증명원 발급 전
        (date(2026, 6, 1), (2024, 2025)),
        (date(2026, 10, 25), (2024, 2025)),
    ],
)
def test_target_income_years(as_of: date, years: tuple[int, int]) -> None:
    assert target_income_years(as_of, income_basis(_rules())) == years


@pytest.mark.parametrize(
    ("amounts", "rule_code", "points"),
    [
        ((25_000_000, 25_000_000), "INCOME_25_30", 50),
        ((30_000_000, 29_999_999), "INCOME_25_30", 50),  # 평균 29,999,999.5원: 반올림하지 않음
        ((30_000_000, 30_000_000), "INCOME_30_35", 65),
        ((40_000_000, 30_000_000), "INCOME_35_40", 80),
        ((44_999_999, 45_000_000), "INCOME_40_45", 95),
        ((45_000_000, 45_000_000), "INCOME_45_50", 110),
        ((50_000_000, 50_000_000), "INCOME_50_PLUS", 120),
    ],
)
def test_income_band_boundaries(amounts: tuple[int, int], rule_code: str, points: int) -> None:
    profile = _profile(incomes=[_income(2024, amounts[0]), _income(2025, amounts[1])])
    result = calculate_income_score(profile, _rules(), AS_OF)
    assert result.rule_code == rule_code
    assert _scores(result) == (ItemStatus.MET, points, points)


def test_income_below_minimum_is_not_met() -> None:
    profile = _profile(incomes=[_income(2024, 25_000_000), _income(2025, 24_999_999)])
    result = calculate_income_score(profile, _rules(), AS_OF)
    assert _scores(result) == (ItemStatus.NOT_MET, 0, 0)
    assert result.message_code == "E74_INCOME_BELOW_MIN"


def test_income_uses_years_before_june() -> None:
    incomes = [_income(2023, 30_000_000), _income(2024, 30_000_000), _income(2025, 50_000_000)]
    result = calculate_income_score(_profile(incomes=incomes), _rules(), date(2026, 5, 31))
    assert result.evidence["incomeYears"] == [2023, 2024]
    assert result.rule_code == "INCOME_30_35"


def test_official_document_wins_over_declared_and_estimate() -> None:
    incomes = [
        _income(2024, 48_000_000, "TRANSACTION_ESTIMATE", "DECLARED"),
        _income(2024, 41_000_000, "USER_DECLARED", "DECLARED"),
        _income(2024, 31_000_000),
        _income(2025, 31_000_000),
    ]
    result = calculate_income_score(_profile(incomes=incomes), _rules(), AS_OF)
    assert result.evidence["sources"] == ["DOCUMENT", "DOCUMENT"]
    assert _scores(result) == (ItemStatus.MET, 65, 65)


@pytest.mark.parametrize(
    ("source", "status"),
    [("TRANSACTION_ESTIMATE", "VERIFIED"), ("USER_DECLARED", "VERIFIED"), ("DOCUMENT", "DECLARED")],
)
def test_only_verified_documents_are_confirmed(source: str, status: str) -> None:
    incomes = [_income(2024, 31_000_000), _income(2025, 31_000_000, source, status)]
    result = calculate_income_score(_profile(incomes=incomes), _rules(), AS_OF)
    assert _scores(result) == (ItemStatus.NEEDS_EVIDENCE, 0, 65)


def test_rejected_record_counts_as_missing() -> None:
    incomes = [_income(2024, 31_000_000), _income(2025, 31_000_000, "DOCUMENT", "REJECTED")]
    result = calculate_income_score(_profile(incomes=incomes), _rules(), AS_OF)
    assert result.status is ItemStatus.NEEDS_EVIDENCE
    assert [m.field for m in result.missing_inputs] == ["incomeRecords[incomeYear=2025]"]


def test_missing_years_are_asked() -> None:
    result = calculate_income_score(_profile(incomes=[_income(2024, 31_000_000)]), _rules(), AS_OF)
    assert _scores(result) == (ItemStatus.NEEDS_EVIDENCE, None, None)
    assert [m.field for m in result.missing_inputs] == ["incomeRecords[incomeYear=2025]"]


def test_fallback_years_use_top_two_and_need_review() -> None:
    incomes = [_income(2021, 36_000_000), _income(2022, 20_000_000), _income(2023, 34_000_000)]
    result = calculate_income_score(_profile(incomes=incomes), _rules(), AS_OF)
    assert result.evidence["incomeYears"] == [2021, 2023]
    assert result.evidence["fallbackUsed"] is True
    assert result.rule_code == "INCOME_35_40"
    assert _scores(result) == (ItemStatus.NEEDS_REVIEW, 0, 80)


def test_agri_fishery_band_needs_review() -> None:
    incomes = [_income(2024, 24_600_000), _income(2025, 24_900_000)]
    farm = calculate_income_score(_profile(incomes=incomes, industry="FISHERY"), _rules(), AS_OF)
    assert farm.rule_code == "INCOME_24_25_AGRI_FISHERY"
    assert _scores(farm) == (ItemStatus.NEEDS_REVIEW, 0, 50)
    factory = calculate_income_score(_profile(incomes=incomes), _rules(), AS_OF)
    assert _scores(factory) == (ItemStatus.NOT_MET, 0, 0)


def test_unknown_industry_in_special_band_is_asked() -> None:
    incomes = [_income(2024, 24_600_000), _income(2025, 24_900_000)]
    result = calculate_income_score(_profile(incomes=incomes, industry=None), _rules(), AS_OF)
    assert result.status is ItemStatus.NEEDS_EVIDENCE
    assert [m.field for m in result.missing_inputs] == ["employmentHistories"]


# --- 한국어 ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "level", "rule_code", "points"),
    [
        ("TOPIK", "2", "KOREAN_L2", 50),
        ("TOPIK", "3급", "KOREAN_L3", 80),
        ("TOPIK", "TOPIK 6급", "KOREAN_L4_PLUS", 120),
        ("KIIP", "2단계", "KOREAN_L2", 50),
        ("KIIP", "4단계", "KOREAN_L4_PLUS", 120),
    ],
)
def test_topik_and_kiip_levels(kind: str, level: str, rule_code: str, points: int) -> None:
    profile = _profile(evidences=[_evidence(1, kind, levelValue=level)])
    result = calculate_korean_score(profile, _rules(), AS_OF)
    assert result.rule_code == rule_code
    assert _scores(result) == (ItemStatus.MET, points, points)


@pytest.mark.parametrize(
    ("score", "rule_code", "points"),
    [
        (41, "KOREAN_L2", 50),
        (60, "KOREAN_L2", 50),
        (61, "KOREAN_L3", 80),
        (81, "KOREAN_L4_PLUS", 120),
    ],
)
def test_pre_test_score_bands(score: int, rule_code: str, points: int) -> None:
    ev = _evidence(
        1, "KIIP_PRE_TEST", preTestScore=score, preTestAssignedStage=3, issuedOn="2025-11-01"
    )
    result = calculate_korean_score(_profile(evidences=[ev]), _rules(), AS_OF)
    assert (result.rule_code, result.earned_points) == (rule_code, points)


def test_pre_test_with_stage_zero_is_not_recognized() -> None:
    ev = _evidence(
        1, "KIIP_PRE_TEST", preTestScore=55, preTestAssignedStage=0, issuedOn="2025-11-01"
    )
    result = calculate_korean_score(_profile(evidences=[ev]), _rules(), AS_OF)
    assert _scores(result) == (ItemStatus.NOT_MET, 0, 0)
    assert result.evidence["candidates"][0]["excluded"] == "ASSIGNED_STAGE_TOO_LOW"


@pytest.mark.parametrize(("result_date", "valid"), [("2024-10-26", True), ("2024-10-25", False)])
def test_pre_test_expires_after_two_years(result_date: str, valid: bool) -> None:
    ev = _evidence(
        1, "KIIP_PRE_TEST", preTestScore=70, preTestAssignedStage=4, issuedOn=result_date
    )
    result = calculate_korean_score(_profile(evidences=[ev]), _rules(), AS_OF)
    assert result.earned_points == (80 if valid else 0)


def test_pre_test_missing_fields_are_asked() -> None:
    ev = _evidence(7, "KIIP_PRE_TEST", preTestScore=55)
    result = calculate_korean_score(_profile(evidences=[ev]), _rules(), AS_OF)
    assert result.status is ItemStatus.NEEDS_EVIDENCE
    assert [m.field for m in result.missing_inputs] == [
        "visaEvidences[visaEvidenceId=7].preTestAssignedStage",
        "visaEvidences[visaEvidenceId=7].issuedOn",
    ]


def test_best_path_wins_and_unverified_higher_level_is_potential() -> None:
    evidences = [
        _evidence(1, "TOPIK", levelValue="2"),
        _evidence(2, "KIIP", "DECLARED", levelValue="4"),
        _evidence(3, "TOPIK", "REJECTED", levelValue="6"),
    ]
    result = calculate_korean_score(_profile(evidences=evidences), _rules(), AS_OF)
    assert result.rule_code == "KOREAN_L4_PLUS"
    assert _scores(result) == (ItemStatus.NEEDS_EVIDENCE, 50, 120)
    assert result.evidence["verifiedRuleCode"] == "KOREAN_L2"
    assert [c["visaEvidenceId"] for c in result.evidence["candidates"]] == [1, 2]


def test_topik_level_1_is_not_met() -> None:
    result = calculate_korean_score(
        _profile(evidences=[_evidence(1, "TOPIK", levelValue="1급")]), _rules(), AS_OF
    )
    assert _scores(result) == (ItemStatus.NOT_MET, 0, 0)


def test_no_korean_evidence_is_asked() -> None:
    result = calculate_korean_score(_profile(), _rules(), AS_OF)
    assert _scores(result) == (ItemStatus.NEEDS_EVIDENCE, None, None)
    assert result.missing_inputs[0].field.startswith("visaEvidences[evidenceType=")


def test_unreadable_level_needs_review() -> None:
    result = calculate_korean_score(
        _profile(evidences=[_evidence(1, "TOPIK", levelValue="중급")]), _rules(), AS_OF
    )
    assert result.status is ItemStatus.NEEDS_REVIEW
    assert result.message_code == "E74_KOREAN_UNREADABLE"

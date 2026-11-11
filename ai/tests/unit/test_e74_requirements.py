from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.visa.e74.engine import E74Assessment, EligibilityStatus, assess_e74
from app.visa.e74.results import ItemResult, ItemStatus
from tests.unit._e74_builders import AS_OF, eligible_body, evidence, job, profile

pytestmark = pytest.mark.unit


def _assess(body: dict[str, Any], as_of: date = AS_OF) -> E74Assessment:
    return assess_e74(profile(body), as_of, include_draft=True)


def _item(assessment: E74Assessment, code: str) -> ItemResult:
    return next(i for i in assessment.items if i.rule_code == code)


def test_base_profile_is_eligible() -> None:
    a = _assess(eligible_body())
    gates = [i for i in a.items if i.category.value in ("REQUIREMENT", "EXCLUSION")]
    assert {i.status for i in gates} <= {
        ItemStatus.MET,
        ItemStatus.NOT_MET,
        ItemStatus.NOT_APPLICABLE,
    }
    assert a.status is EligibilityStatus.ELIGIBLE


# --- 체류 ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("started", "prior", "expected"),
    [
        ("2022-10-25", None, ItemStatus.MET),  # 현재 체류만 48개월
        ("2022-10-26", None, ItemStatus.NEEDS_EVIDENCE),  # 47개월 → 이전 체류를 묻는다
        ("2022-10-26", 1, ItemStatus.MET),
        ("2022-10-26", 0, ItemStatus.NOT_MET),
        ("2010-01-01", None, ItemStatus.MET),  # 10년 범위로 잘라도 120개월
    ],
)
def test_stay_period(started: str, prior: int | None, expected: ItemStatus) -> None:
    body = eligible_body()
    body["visaProfile"] |= {"currentStayStartedOn": started, "priorStayMonths": prior}
    item = _item(_assess(body), "REQ_STAY_PERIOD")
    assert item.status is expected
    if expected is ItemStatus.NEEDS_EVIDENCE:
        assert [m.field for m in item.missing_inputs] == ["visaProfile.priorStayMonths"]


def test_stay_start_missing_is_asked() -> None:
    body = eligible_body()
    del body["visaProfile"]["currentStayStartedOn"]
    a = _assess(body)
    assert _item(a, "REQ_STAY_PERIOD").missing_inputs[0].field == "visaProfile.currentStayStartedOn"
    assert a.status is EligibilityStatus.NEEDS_EVIDENCE


@pytest.mark.parametrize(
    ("region", "expected"), [("46720", ItemStatus.NEEDS_REVIEW), ("41110", ItemStatus.NOT_MET)]
)
def test_non_capital_stay_special_needs_review(region: str, expected: ItemStatus) -> None:
    body = eligible_body()
    body["visaProfile"] |= {"currentStayStartedOn": "2023-09-01", "priorStayMonths": 0}
    job(body)["workplaceRegionCode"] = region
    body["visaEvidences"].append({"visaEvidenceId": 104, "evidenceType": "RECOMMENDATION_PROVINCE"})
    item = _item(_assess(body), "REQ_STAY_PERIOD")
    assert item.status is expected
    if expected is ItemStatus.NEEDS_REVIEW:
        assert item.review_note is not None and item.review_note.startswith("Q8")


def test_declared_profile_needs_evidence() -> None:
    body = eligible_body()
    body["visaProfile"]["evidenceStatus"] = "DECLARED"
    a = _assess(body)
    assert _item(a, "REQ_STAY_PERIOD").status is ItemStatus.NEEDS_EVIDENCE
    assert _item(a, "REQ_CURRENT_EMPLOYMENT").status is ItemStatus.NEEDS_EVIDENCE


# --- 현재 근무 ------------------------------------------------------------------------


def test_current_employment() -> None:
    body = eligible_body()
    body["visaProfile"]["visaTypeCode"] = "E-7"
    assert _item(_assess(body), "REQ_CURRENT_EMPLOYMENT").message_code == "E74_REQ_VISA_TYPE"

    body = eligible_body()
    job(body)["endedOn"] = "2026-09-30"
    a = _assess(body)
    assert _item(a, "REQ_CURRENT_EMPLOYMENT").status is ItemStatus.NOT_MET
    assert a.status is EligibilityStatus.INELIGIBLE


# --- 고용계약 -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("end", "salary", "message"),
    [
        ("2028-10-24", 26_000_000, "E74_REQ_CONTRACT"),  # 기준일부터 꼭 2년
        ("2028-10-23", 26_000_000, "E74_REQ_CONTRACT_TOO_SHORT"),
        ("2028-11-30", 25_999_999, "E74_REQ_CONTRACT_SALARY_LOW"),
    ],
)
def test_contract_boundaries(end: str, salary: int, message: str) -> None:
    body = eligible_body()
    job(body).update({"contractEndOn": end, "contractAnnualSalary": salary})
    item = _item(_assess(body), "REQ_CONTRACT")
    assert item.message_code == message
    assert item.status is (ItemStatus.MET if message == "E74_REQ_CONTRACT" else ItemStatus.NOT_MET)


def test_agri_fishery_contract_row_applies_for_farm_workers() -> None:
    body = eligible_body()
    job(body).update({"industryCode": "LIVESTOCK", "contractAnnualSalary": 25_000_000})
    a = _assess(body)
    assert _item(a, "REQ_CONTRACT_AGRI_FISHERY").status is ItemStatus.MET
    general = _item(a, "REQ_CONTRACT")
    assert general.status is ItemStatus.NOT_APPLICABLE
    assert general.evidence["appliedRuleCode"] == "REQ_CONTRACT_AGRI_FISHERY"


def test_missing_contract_fields_are_asked() -> None:
    body = eligible_body()
    job(body).update({"contractEndOn": None, "contractAnnualSalary": None})
    item = _item(_assess(body), "REQ_CONTRACT")
    assert [m.field for m in item.missing_inputs] == [
        "employmentHistories[employmentHistoryId=11].contractEndOn",
        "employmentHistories[employmentHistoryId=11].contractAnnualSalary",
    ]


# --- 기업 추천 -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("started", "expected"), [("2025-10-25", ItemStatus.MET), ("2025-10-26", ItemStatus.NOT_MET)]
)
def test_employer_recommendation_needs_one_year(started: str, expected: ItemStatus) -> None:
    body = eligible_body()
    job(body)["startedOn"] = started
    body["visaProfile"]["currentStayStartedOn"] = "2021-04-01"
    assert _item(_assess(body), "REQ_EMPLOYER_REC").status is expected


def test_missing_employer_recommendation_is_asked_not_failed() -> None:
    body = eligible_body()
    body["visaEvidences"].remove(evidence(body, "RECOMMENDATION_EMPLOYER"))
    a = _assess(body)
    item = _item(a, "REQ_EMPLOYER_REC")
    assert item.status is ItemStatus.NEEDS_EVIDENCE
    assert item.missing_inputs[0].field == "visaEvidences[evidenceType=RECOMMENDATION_EMPLOYER]"
    assert a.status is EligibilityStatus.NEEDS_EVIDENCE


def test_employer_tenure_asks_job_change_reason_when_it_would_help() -> None:
    body = eligible_body()
    job(body)["startedOn"] = "2026-03-01"
    body["employmentHistories"].append(
        {
            "employmentHistoryId": 10,
            "employerName": "옛공장",
            "industryCode": "MANUFACTURING",
            "startedOn": "2024-01-01",
            "endedOn": "2026-02-27",
            "verificationStatus": "VERIFIED",
        }
    )
    item = _item(_assess(body), "REQ_EMPLOYER_REC")
    assert (
        item.missing_inputs[0].field
        == "employmentHistories[employmentHistoryId=10].leftWithoutFault"
    )


# --- 소득·한국어 ------------------------------------------------------------------------


def test_income_below_minimum_makes_ineligible() -> None:
    body = eligible_body()
    for record in body["incomeRecords"]:
        record["annualAmount"] = 24_900_000
    a = _assess(body)
    assert _item(a, "REQ_MIN_INCOME").status is ItemStatus.NOT_MET
    assert a.status is EligibilityStatus.INELIGIBLE


def test_agri_fishery_income_band_needs_review() -> None:
    body = eligible_body()
    job(body)["industryCode"] = "AGRICULTURE"
    body["incomeRecords"][0]["annualAmount"] = 24_600_000
    body["incomeRecords"][1]["annualAmount"] = 24_900_000
    a = _assess(body)
    assert _item(a, "REQ_MIN_INCOME_AGRI_FISHERY").status is ItemStatus.NEEDS_REVIEW
    assert _item(a, "REQ_MIN_INCOME").status is ItemStatus.NOT_APPLICABLE
    assert a.status is EligibilityStatus.NEEDS_REVIEW


def test_korean_deferral_applies_when_other_points_reach_150() -> None:
    body = eligible_body()
    evidence(body, "TOPIK")["levelValue"] = "1"
    a = _assess(body)
    item = _item(a, "REQ_MIN_KOREAN")
    assert (item.status, item.message_code) == (ItemStatus.MET, "E74_KOREAN_DEFERRED")
    assert a.korean_deferral_applied is True
    assert a.score.required == 150
    assert a.status is EligibilityStatus.ELIGIBLE


def test_korean_deferral_does_not_apply_below_150() -> None:
    body = eligible_body()
    evidence(body, "TOPIK")["levelValue"] = "1"
    body["visaEvidences"].remove(evidence(body, "DRIVER_LICENSE"))
    body["visaProfile"]["birthDate"] = "1980-01-01"  # 10점
    job(body).update(
        {"startedOn": "2025-01-01", "isDepopulationArea": False}
    )  # 근속·지역 가점 없음
    for record in body["incomeRecords"]:
        record["annualAmount"] = 26_000_000  # 50점
    a = _assess(body)  # 한국어 외 총점 50 + 10 + 50 = 110
    assert _item(a, "REQ_MIN_KOREAN").status is ItemStatus.NOT_MET
    assert a.status is EligibilityStatus.INELIGIBLE


# --- 총점 ----------------------------------------------------------------------------


def test_unknown_penalties_keep_total_unconfirmed() -> None:
    body = eligible_body()
    body["legalDeclaration"]["finesUnder1mCount"] = None
    item = _item(_assess(body), "REQ_TOTAL_SCORE")
    assert item.status is ItemStatus.NEEDS_EVIDENCE


def test_total_met_only_with_declared_points_needs_evidence() -> None:
    body = eligible_body()
    evidence(body, "TOPIK")["status"] = "DECLARED"  # 확정 225 + 가능 80
    body["visaEvidences"].append(
        {"visaEvidenceId": 105, "evidenceType": "RECOMMENDATION_MINISTRY", "status": "DECLARED"}
    )
    for record in body["incomeRecords"]:
        record["verificationStatus"] = "DECLARED"  # 확정 160
    a = _assess(body)
    assert a.score.total == 160 and a.potential_score.total == 335
    assert _item(a, "REQ_TOTAL_SCORE").status is ItemStatus.NEEDS_EVIDENCE


def test_total_below_200_with_everything_known_is_not_met() -> None:
    body = eligible_body()
    body["legalDeclaration"] |= {
        "finesUnder1mCount": 3,
        "taxStayRestrictionCount": 3,
        "immigrationViolationCount": 3,
    }
    body["visaEvidences"].remove(evidence(body, "DRIVER_LICENSE"))
    job(body)["isDepopulationArea"] = False
    a = _assess(body)  # 305 - 10 - 20 - 50 = 225
    assert a.score.total == 225
    assert _item(a, "REQ_TOTAL_SCORE").status is ItemStatus.MET
    for record in body["incomeRecords"]:
        record["annualAmount"] = 25_000_000  # 65 → 50
    body["visaProfile"]["birthDate"] = "1980-01-01"  # 60 → 10
    a = _assess(body)  # 225 - 15 - 50 = 160
    assert a.score.total == 160
    assert _item(a, "REQ_TOTAL_SCORE").status is ItemStatus.NOT_MET
    assert a.score_requirement_met is False


# --- 제외 -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("hasFine1mOrMore", True, "EXC_FINE_1M_PLUS"),
        ("hasCurrentTaxArrears", True, "EXC_TAX_ARREARS"),
        ("immigrationViolationCount", 4, "EXC_IMMIGRATION_4PLUS"),
        ("hasOverstay3mOrMore", True, "EXC_OVERSTAY_3M"),
    ],
)
def test_exclusions_make_ineligible(field: str, value: Any, code: str) -> None:
    body = eligible_body()
    body["legalDeclaration"][field] = value
    a = _assess(body)
    assert _item(a, code).status is ItemStatus.MET
    assert a.status is EligibilityStatus.INELIGIBLE


def test_undeclared_exclusion_is_asked() -> None:
    body = eligible_body()
    body["legalDeclaration"]["hasCurrentTaxArrears"] = None
    a = _assess(body)
    assert (
        _item(a, "EXC_TAX_ARREARS").missing_inputs[0].field
        == "legalDeclaration.hasCurrentTaxArrears"
    )
    assert a.status is EligibilityStatus.NEEDS_EVIDENCE


def test_discretionary_exclusions_are_not_judged() -> None:
    a = _assess(eligible_body())
    for code in ("EXC_PUBLIC_SAFETY", "EXC_SOCIAL_ORDER"):
        item = _item(a, code)
        assert (item.status, item.message_code) == (
            ItemStatus.NOT_APPLICABLE,
            "E74_EXCLUSION_MANUAL",
        )

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.visa.e74.engine import E74Assessment, assess_e74
from app.visa.e74.improvements import Improvement, ImprovementKind
from app.visa.e74.rules import RuleActor
from tests.unit._e74_builders import AS_OF, eligible_body, evidence, job, profile

pytestmark = pytest.mark.unit


def _assess(body: dict[str, Any], as_of: date = AS_OF) -> E74Assessment:
    return assess_e74(profile(body), as_of, include_draft=True)


def _by_code(a: E74Assessment) -> dict[str, Improvement]:
    return {imp.rule_code: imp for imp in a.improvements}


def test_eligible_profile_gets_optional_improvements_only() -> None:
    a = _assess(eligible_body())
    assert a.score_gap.model_dump() == {
        "required": 200,
        "points_needed": 0,
        "points_needed_after_evidence": 0,
    }
    assert a.plan_to_required.rule_codes == () and a.plan_to_required.reaches_required
    improvements = _by_code(a)
    assert improvements["KOREAN_L4_PLUS"].points_gain == 40
    assert improvements["INCOME_35_40"].target == {
        "averageIncomeNeeded": 35_000_000,
        "currentAverage": 32_650_000,
        "shortfall": 2_350_000,
    }
    group = improvements["REC_CENTRAL"]
    assert group.target["alternatives"] == ["REC_CENTRAL", "REC_REGIONAL"]
    assert group.actor is RuleActor.GOVERNMENT
    assert all(not imp.blocking for imp in a.improvements)


def test_nothing_is_suggested_for_age_or_past_penalties() -> None:
    body = eligible_body()
    body["legalDeclaration"]["finesUnder1mCount"] = 2
    codes = {imp.rule_code for imp in _assess(body).improvements}
    assert not any(code.startswith(("AGE_", "PEN_")) for code in codes)


def test_obtained_government_recommendation_is_not_suggested_again() -> None:
    body = eligible_body()
    body["visaEvidences"].append(
        {"visaEvidenceId": 104, "evidenceType": "RECOMMENDATION_PROVINCE", "status": "VERIFIED"}
    )
    codes = {imp.rule_code for imp in _assess(body).improvements}
    assert "REC_CENTRAL" not in codes and "REC_REGIONAL" not in codes


def test_declared_items_suggest_submitting_documents() -> None:
    body = eligible_body()
    evidence(body, "TOPIK")["status"] = "DECLARED"
    job(body)["verificationStatus"] = "DECLARED"
    improvements = _by_code(_assess(body))
    korean = improvements["KOREAN_L3"]
    assert (korean.kind, korean.points_gain, korean.blocking) == (
        ImprovementKind.SUBMIT_EVIDENCE,
        80,
        False,
    )
    assert korean.documents == ("TOPIK 성적표 또는 사회통합프로그램 이수증·사전평가 성적표",)
    contract = improvements["REQ_CONTRACT"]
    assert (contract.kind, contract.points_gain, contract.blocking) == (
        ImprovementKind.SUBMIT_EVIDENCE,
        None,
        True,
    )
    assert contract.documents == ("E-7-4 고용계약서(붙임9 견본)",)


def test_bonuses_reached_by_waiting_have_a_date() -> None:
    body = eligible_body()
    job(body).update({"startedOn": "2024-06-01"})
    body["visaProfile"]["currentStayStartedOn"] = "2021-04-01"
    improvements = _by_code(_assess(body))
    tenure = improvements["TENURE_3Y"]
    assert (tenure.kind, tenure.available_from, tenure.points_gain) == (
        ImprovementKind.WAIT_UNTIL,
        date(2027, 6, 1),
        20,
    )
    assert improvements["REGION_3Y"].available_from == date(2027, 6, 1)


def test_region_bonus_is_not_suggested_outside_such_areas() -> None:
    body = eligible_body()
    job(body).update({"isDepopulationArea": False})
    assert "REGION_3Y" not in _by_code(_assess(body))


@pytest.mark.parametrize(
    ("change", "code", "expected"),
    [
        (
            {"contractAnnualSalary": 25_500_000},
            "REQ_CONTRACT",
            {"requiredAnnualSalary": 26_000_000, "annualSalary": 25_500_000, "shortfall": 500_000},
        ),
        (
            {"contractEndOn": "2027-12-31"},
            "REQ_CONTRACT",
            {"requiredEndOn": "2028-10-24", "contractEndOn": "2027-12-31"},
        ),
    ],
)
def test_contract_fixes(change: dict[str, Any], code: str, expected: dict[str, Any]) -> None:
    body = eligible_body()
    job(body).update(change)
    a = _assess(body)
    first = a.improvements[0]
    assert (first.rule_code, first.kind, first.blocking) == (
        code,
        ImprovementKind.FIX_REQUIREMENT,
        True,
    )
    assert first.actor is RuleActor.EMPLOYER
    assert first.target == expected


def test_stay_and_employer_tenure_fixes_say_when() -> None:
    body = eligible_body()
    body["visaProfile"].update({"currentStayStartedOn": "2023-03-01", "priorStayMonths": 0})
    job(body).update({"startedOn": "2026-03-01"})
    improvements = _by_code(_assess(body))
    assert improvements["REQ_STAY_PERIOD"].available_from == date(2027, 3, 1)
    assert improvements["REQ_EMPLOYER_REC"].available_from == date(2027, 3, 1)


def test_score_shortfall_comes_with_a_plan() -> None:
    body = eligible_body()
    body["visaEvidences"].remove(evidence(body, "DRIVER_LICENSE"))
    job(body).update({"isDepopulationArea": False, "startedOn": "2025-01-01"})
    for record in body["incomeRecords"]:
        record["annualAmount"] = 25_000_000
    body["visaProfile"]["birthDate"] = "1980-01-01"
    a = _assess(body)  # 50 + 80 + 10 + 50 = 190
    assert a.score.total == 190 and a.score_gap.points_needed == 10
    total_fix = _by_code(a)["REQ_TOTAL_SCORE"]
    assert total_fix.target["pointsNeeded"] == 10 and total_fix.blocking
    assert a.plan_to_required.reaches_required
    assert a.plan_to_required.points_added >= 10
    assert a.plan_to_required.rule_codes[0] == a.improvements[1].rule_code


def test_improvements_are_ordered_by_kind_then_date_then_gain() -> None:
    body = eligible_body()
    job(body).update({"contractAnnualSalary": 25_500_000, "startedOn": "2024-06-01"})
    evidence(body, "DRIVER_LICENSE")["status"] = "DECLARED"
    kinds = [imp.kind for imp in _assess(body).improvements]
    order = [
        ImprovementKind.FIX_REQUIREMENT,
        ImprovementKind.SUBMIT_EVIDENCE,
        ImprovementKind.WAIT_UNTIL,
        ImprovementKind.RAISE_LEVEL,
    ]

    def rank(kind: ImprovementKind) -> int:
        return order.index(kind) if kind in order else len(order) - 1

    assert kinds == sorted(kinds, key=rank)
    assert kinds[0] is ImprovementKind.FIX_REQUIREMENT


def test_missing_korean_is_asked_before_suggesting_a_level() -> None:
    body = eligible_body()
    body["visaEvidences"].remove(evidence(body, "TOPIK"))
    a = _assess(body)
    assert not any(imp.rule_code.startswith("KOREAN_") for imp in a.improvements)
    assert any(m.field.startswith("visaEvidences[evidenceType=TOPIK") for m in a.missing_inputs)


def test_no_rule_set_means_no_improvements() -> None:
    a = _assess(eligible_body(), date(2027, 3, 1))
    assert a.improvements == () and a.score_gap.required is None
    assert not a.plan_to_required.reaches_required


def _without_employer_recommendation(body: dict[str, Any]) -> dict[str, Any]:
    body["visaEvidences"] = [
        e for e in body["visaEvidences"] if e["evidenceType"] != "RECOMMENDATION_EMPLOYER"
    ]
    return body


def test_missing_employer_recommendation_blocks_application() -> None:
    # REQ_EMPLOYER_REC가 NEEDS_EVIDENCE(근속은 채움, 추천서 없음)
    a = _assess(_without_employer_recommendation(eligible_body()))
    assert next(i for i in a.items if i.rule_code == "REQ_EMPLOYER_REC").status.value == (
        "NEEDS_EVIDENCE"
    )
    rec = _by_code(a)["REC_EMPLOYER"]
    assert (rec.kind, rec.points_gain, rec.blocking) == (ImprovementKind.OBTAIN_BONUS, 50, True)


def test_employer_recommendation_blocks_when_tenure_is_short() -> None:
    # REQ_EMPLOYER_REC가 NOT_MET(현재 근무처 1년 미만)
    body = _without_employer_recommendation(eligible_body())
    job(body).update({"startedOn": "2026-03-01"})
    a = _assess(body)
    assert next(i for i in a.items if i.rule_code == "REQ_EMPLOYER_REC").status.value == "NOT_MET"
    assert _by_code(a)["REC_EMPLOYER"].blocking


def test_declared_employer_recommendation_blocks_until_verified() -> None:
    body = eligible_body()
    evidence(body, "RECOMMENDATION_EMPLOYER")["status"] = "DECLARED"
    rec = _by_code(_assess(body))["REC_EMPLOYER"]
    assert (rec.kind, rec.blocking) == (ImprovementKind.SUBMIT_EVIDENCE, True)

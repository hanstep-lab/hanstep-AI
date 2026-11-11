from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from app.visa.e74.engine import E74Assessment, EligibilityStatus, assess_e74, request_key
from app.visa.e74.rules import ApprovalStatus, RuleCategory, default_rule_sets
from app.visa.e74.schemas import E74Profile
from tests.unit._e74_builders import AS_OF, eligible_body, profile, rules_2026
from tests.unit._e74_cases import profile_json

pytestmark = pytest.mark.unit


def _assess(body: dict[str, Any], as_of: date = AS_OF) -> E74Assessment:
    return assess_e74(profile(body), as_of, include_draft=True)


def test_eligible_profile_summary() -> None:
    a = _assess(eligible_body())
    assert a.status is EligibilityStatus.ELIGIBLE and a.eligibility_confirmed
    assert a.rule_version == "E74-2026-v1"
    assert a.score.model_dump() == {
        "base": 205,
        "bonus": 100,
        "penalty": 0,
        "total": 305,
        "required": 200,
    }
    assert a.potential_score.total == 305 and a.potential_score.complete
    assert a.score_requirement_met is True
    assert a.missing_inputs == () and a.needs_evidence == () and a.review_notes == ()


def test_one_item_per_rule_in_category_order() -> None:
    a = _assess(eligible_body())
    codes = [item.rule_code for item in a.items]
    assert len(codes) == len(set(codes)) == 28  # 기본 3 + 가점 7 + 감점 3 + 요건 9 + 제외 6
    categories = [item.category for item in a.items]
    order = [
        RuleCategory.BASE_INCOME,
        RuleCategory.BASE_KOREAN,
        RuleCategory.BASE_AGE,
        RuleCategory.BONUS,
        RuleCategory.PENALTY,
        RuleCategory.REQUIREMENT,
        RuleCategory.EXCLUSION,
    ]
    assert categories == sorted(categories, key=order.index)


def test_sample_profiles() -> None:
    p01 = assess_e74(E74Profile.model_validate(profile_json("P01")), AS_OF, include_draft=True)
    assert p01.status is EligibilityStatus.ELIGIBLE
    assert (p01.score.total, p01.potential_score.total) == (305, 325)
    assert p01.needs_evidence == ("QUALIFICATION",)

    p02 = assess_e74(E74Profile.model_validate(profile_json("P02")), AS_OF, include_draft=True)
    assert p02.status is EligibilityStatus.NEEDS_EVIDENCE
    assert not p02.potential_score.complete
    assert "REQ_TOTAL_SCORE" not in p02.needs_evidence  # 계산되는 요건은 서류 안내 대상이 아님

    p03 = assess_e74(E74Profile.model_validate(profile_json("P03")), AS_OF, include_draft=True)
    assert p03.status is EligibilityStatus.NEEDS_EVIDENCE
    assert [n.rule_code for n in p03.review_notes] == [
        "INCOME_24_25_AGRI_FISHERY",
        "REQ_MIN_INCOME_AGRI_FISHERY",
    ]


def test_missing_inputs_are_merged_by_field() -> None:
    a = assess_e74(E74Profile.model_validate(profile_json("P02")), AS_OF, include_draft=True)
    fields = [m.field for m in a.missing_inputs]
    assert len(fields) == len(set(fields))
    immigration = next(
        m for m in a.missing_inputs if m.field == "legalDeclaration.immigrationViolationCount"
    )
    assert immigration.rule_codes == ("PEN_IMMIGRATION", "EXC_IMMIGRATION_4PLUS")
    employment = next(m for m in a.missing_inputs if m.field == "employmentHistories")
    assert {"TENURE_3Y", "REQ_CONTRACT", "REQ_EMPLOYER_REC"} <= set(employment.rule_codes)


def test_ineligible_beats_missing_information() -> None:
    body = eligible_body()
    body["legalDeclaration"] |= {"hasFine1mOrMore": True, "hasCurrentTaxArrears": None}
    assert _assess(body).status is EligibilityStatus.INELIGIBLE


def test_missing_information_beats_review() -> None:
    body = eligible_body()
    body["employmentHistories"][0]["industryCode"] = "AGRICULTURE"
    body["incomeRecords"][0]["annualAmount"] = 24_600_000
    body["incomeRecords"][1]["annualAmount"] = 24_900_000
    assert _assess(body).status is EligibilityStatus.NEEDS_REVIEW
    body["legalDeclaration"]["hasCurrentTaxArrears"] = None
    assert _assess(body).status is EligibilityStatus.NEEDS_EVIDENCE


def test_no_rule_set_for_the_date_needs_review() -> None:
    a = _assess(eligible_body(), date(2027, 3, 1))
    assert a.status is EligibilityStatus.NEEDS_REVIEW
    assert a.rule_version is None and a.items == ()
    assert a.review_notes[0].note.endswith("(Q9)")


def test_draft_rules_are_not_used_by_default() -> None:
    a = assess_e74(profile(eligible_body()), AS_OF)
    assert a.status is EligibilityStatus.NEEDS_REVIEW and a.rule_version is None


def test_approved_rules_are_used_without_draft_flag() -> None:
    approved = rules_2026().model_copy(update={"approval_status": ApprovalStatus.APPROVED})
    a = assess_e74(profile(eligible_body()), AS_OF, rule_sets=[approved])
    assert a.status is EligibilityStatus.ELIGIBLE


# --- requestKey(결과 누적·재사용 키) ------------------------------------------------------


def test_same_input_gives_same_key_even_if_rows_are_reordered() -> None:
    body = eligible_body()
    first = _assess(body).request_key
    body["visaEvidences"].reverse()
    body["incomeRecords"].reverse()
    assert _assess(body).request_key == first
    assert len(first) == 64


def test_key_changes_with_input_date_or_rule_version() -> None:
    base = _assess(eligible_body()).request_key
    changed = eligible_body()
    changed["incomeRecords"][0]["annualAmount"] += 1
    assert _assess(changed).request_key != base
    assert _assess(eligible_body(), date(2026, 10, 26)).request_key != base
    other_version = request_key("E74-2026-v2", AS_OF, profile(eligible_body()))
    assert other_version != base


def test_omitted_and_null_fields_give_the_same_key() -> None:
    with_null = eligible_body()
    with_null["visaProfile"]["priorStayMonths"] = None
    assert _assess(with_null).request_key == _assess(eligible_body()).request_key


def test_results_cannot_be_modified_after_calculation() -> None:
    a = _assess(eligible_body())
    with pytest.raises(ValidationError):
        a.status = EligibilityStatus.INELIGIBLE  # type: ignore[misc]
    with pytest.raises(ValidationError):
        a.items[0].earned_points = 999  # type: ignore[misc]


def test_default_rule_sets_are_loaded_once() -> None:
    assert default_rule_sets() is default_rule_sets()

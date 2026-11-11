from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.visa.e74.bonus_penalty import (
    calculate_bonus_items,
    calculate_license_bonus,
    calculate_penalties,
    calculate_qualification_bonus,
    calculate_recommendation_bonus,
    calculate_region_bonus,
    calculate_tenure_bonus,
)
from app.visa.e74.employment import covered_days, normalize_employer_name, required_days
from app.visa.e74.results import ItemResult, ItemStatus
from app.visa.e74.rules import RuleSet, default_rule_sets, select_rule_set
from app.visa.e74.schemas import E74Profile

pytestmark = pytest.mark.unit

AS_OF = date(2026, 10, 25)


def _rules() -> RuleSet:
    rule_set = select_rule_set(AS_OF, default_rule_sets(), include_draft=True)
    assert rule_set is not None
    return rule_set


def _job(
    job_id: int,
    started: str,
    ended: str | None = None,
    *,
    name: str = "한빛정밀",
    status: str = "VERIFIED",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "employmentHistoryId": job_id,
        "employerName": name,
        "industryCode": "MANUFACTURING",
        "startedOn": started,
        "endedOn": ended,
        "verificationStatus": status,
        **extra,
    }


def _ev(evidence_id: int, kind: str, status: str = "VERIFIED", **extra: Any) -> dict[str, Any]:
    return {"visaEvidenceId": evidence_id, "evidenceType": kind, "status": status, **extra}


def _profile(
    jobs: list[dict[str, Any]] | None = None,
    evidences: list[dict[str, Any]] | None = None,
    declaration: dict[str, Any] | None = None,
) -> E74Profile:
    body: dict[str, Any] = {
        "visaProfile": {"visaTypeCode": "E-9"},
        "employmentHistories": jobs or [],
        "visaEvidences": evidences or [],
    }
    if declaration is not None:
        body["legalDeclaration"] = declaration
    return E74Profile.model_validate(body)


def _scores(item: ItemResult | None) -> tuple[ItemStatus, int | None, int | None]:
    assert item is not None
    return item.status, item.earned_points, item.potential_points


def _by_code(items: list[ItemResult]) -> dict[str | None, ItemResult]:
    return {item.rule_code: item for item in items}


# --- 기간 도우미 ---------------------------------------------------------------------


def test_employer_name_normalization() -> None:
    assert normalize_employer_name("(주)한빛 정밀") == normalize_employer_name("한빛정밀 주식회사")
    assert normalize_employer_name("㈜한빛정밀") == "한빛정밀"


def test_covered_days_merges_overlaps_and_touching_intervals() -> None:
    a = (date(2020, 1, 1), date(2020, 1, 11))
    b = (date(2020, 1, 6), date(2020, 1, 16))
    c = (date(2020, 1, 16), date(2020, 1, 21))
    assert covered_days([a, b, c]) == 20
    assert covered_days([]) == 0


def test_required_days_matches_calendar_months() -> None:
    assert required_days(AS_OF, 36) == (AS_OF - date(2023, 10, 25)).days


# --- 추천 ------------------------------------------------------------------------


def test_central_and_regional_count_once() -> None:
    evidences = [_ev(1, "RECOMMENDATION_MINISTRY"), _ev(2, "RECOMMENDATION_PROVINCE")]
    items = _by_code(calculate_recommendation_bonus(_profile(evidences=evidences), _rules()))
    assert _scores(items["REC_CENTRAL"]) == (ItemStatus.MET, 30, 30)
    regional = items["REC_REGIONAL"]
    assert _scores(regional) == (ItemStatus.NOT_APPLICABLE, 0, 0)
    assert regional.evidence["countedRuleCode"] == "REC_CENTRAL"
    assert regional.evidence["ownStatus"] == "MET"


def test_verified_recommendation_wins_the_group() -> None:
    evidences = [
        _ev(1, "RECOMMENDATION_MINISTRY", "DECLARED"),
        _ev(2, "RECOMMENDATION_PROVINCE"),
    ]
    items = _by_code(calculate_recommendation_bonus(_profile(evidences=evidences), _rules()))
    assert _scores(items["REC_REGIONAL"]) == (ItemStatus.MET, 30, 30)
    assert items["REC_CENTRAL"].status is ItemStatus.NOT_APPLICABLE


def test_declared_recommendation_is_potential_only() -> None:
    evidences = [
        _ev(1, "RECOMMENDATION_EMPLOYER", "DECLARED"),
        _ev(2, "RECOMMENDATION_PROVINCE", "REJECTED"),
    ]
    items = _by_code(calculate_recommendation_bonus(_profile(evidences=evidences), _rules()))
    assert _scores(items["REC_EMPLOYER"]) == (ItemStatus.NEEDS_EVIDENCE, 0, 50)
    assert _scores(items["REC_REGIONAL"]) == (ItemStatus.NOT_MET, 0, 0)


# --- 현 근무처 근속 -------------------------------------------------------------------


@pytest.mark.parametrize(("started", "met"), [("2023-10-25", True), ("2023-10-26", False)])
def test_tenure_three_year_boundary(started: str, met: bool) -> None:
    item = calculate_tenure_bonus(_profile([_job(1, started)]), _rules(), AS_OF)
    assert _scores(item) == ((ItemStatus.MET, 20, 20) if met else (ItemStatus.NOT_MET, 0, 0))


def test_tenure_adds_earlier_stint_at_same_employer() -> None:
    jobs = [
        _job(1, "2019-01-01", "2020-12-31", name="(주)한빛정밀"),
        _job(2, "2021-01-01", "2025-10-24", name="다른공장"),
        _job(3, "2025-10-25", name="한빛정밀 주식회사"),
    ]
    item = calculate_tenure_bonus(_profile(jobs), _rules(), AS_OF)
    assert _scores(item) == (ItemStatus.MET, 20, 20)
    assert item is not None and item.evidence["employmentHistoryIds"] == [1, 3]


@pytest.mark.parametrize(
    ("left_without_fault", "expected"),
    [
        (True, (ItemStatus.MET, 20, 20)),
        (False, (ItemStatus.NOT_MET, 0, 0)),
        (None, (ItemStatus.NEEDS_EVIDENCE, None, None)),
    ],
)
def test_tenure_adds_previous_job_only_when_left_without_fault(
    left_without_fault: bool | None, expected: tuple[ItemStatus, int | None, int | None]
) -> None:
    jobs = [
        _job(1, "2022-01-01", "2024-12-31", name="옛공장", leftWithoutFault=left_without_fault),
        _job(2, "2025-01-02"),
    ]
    item = calculate_tenure_bonus(_profile(jobs), _rules(), AS_OF)
    assert _scores(item) == expected
    if left_without_fault is None:
        assert item is not None
        assert [m.field for m in item.missing_inputs] == [
            "employmentHistories[employmentHistoryId=1].leftWithoutFault"
        ]


def test_tenure_does_not_ask_when_previous_job_would_not_help() -> None:
    jobs = [_job(1, "2024-01-01", "2024-03-31", name="옛공장"), _job(2, "2025-01-02")]
    assert _scores(calculate_tenure_bonus(_profile(jobs), _rules(), AS_OF)) == (
        ItemStatus.NOT_MET,
        0,
        0,
    )


def test_tenure_without_history_or_current_job() -> None:
    missing = calculate_tenure_bonus(_profile(), _rules(), AS_OF)
    assert missing is not None and [m.field for m in missing.missing_inputs] == [
        "employmentHistories"
    ]
    no_current = calculate_tenure_bonus(
        _profile([_job(1, "2019-01-01", "2024-01-01")]), _rules(), AS_OF
    )
    assert no_current is not None and no_current.message_code == "E74_TENURE_NO_CURRENT_JOB"


def test_unverified_tenure_is_potential_only() -> None:
    item = calculate_tenure_bonus(
        _profile([_job(1, "2020-01-01", status="DECLARED")]), _rules(), AS_OF
    )
    assert _scores(item) == (ItemStatus.NEEDS_EVIDENCE, 0, 20)


# --- 인구감소지역·읍면 근무 ------------------------------------------------------------


@pytest.mark.parametrize(("ended", "met"), [("2019-12-31", True), ("2019-06-30", False)])
def test_region_counts_only_last_ten_years(ended: str, met: bool) -> None:
    jobs = [
        _job(1, "2014-01-01", ended, isDepopulationArea=True),
        _job(2, "2024-01-01", isDepopulationArea=False),
    ]
    item = calculate_region_bonus(_profile(jobs), _rules(), AS_OF)
    assert _scores(item) == ((ItemStatus.MET, 20, 20) if met else (ItemStatus.NOT_MET, 0, 0))
    assert item is not None and item.evidence["windowStart"] == "2016-10-25"


def test_region_sums_separate_jobs() -> None:
    jobs = [
        _job(1, "2018-01-01", "2019-12-31", name="가", isDepopulationArea=True),
        _job(2, "2020-01-01", "2023-12-31", name="나", isDepopulationArea=False),
        _job(3, "2024-01-01", name="다", isDepopulationArea=True),
    ]
    assert _scores(calculate_region_bonus(_profile(jobs), _rules(), AS_OF)) == (
        ItemStatus.MET,
        20,
        20,
    )


def test_region_asks_about_unknown_rows_that_would_complete_three_years() -> None:
    jobs = [
        _job(1, "2018-01-01", "2019-12-31", name="가", isDepopulationArea=True),
        _job(2, "2024-01-01", name="다", isDepopulationArea=None),
    ]
    item = calculate_region_bonus(_profile(jobs), _rules(), AS_OF)
    assert _scores(item) == (ItemStatus.NEEDS_EVIDENCE, None, None)
    assert item is not None
    assert [m.field for m in item.missing_inputs] == [
        "employmentHistories[employmentHistoryId=2].isDepopulationArea"
    ]


def test_region_does_not_ask_when_unknown_rows_would_not_help() -> None:
    jobs = [_job(1, "2025-01-01", isDepopulationArea=None)]
    assert _scores(calculate_region_bonus(_profile(jobs), _rules(), AS_OF)) == (
        ItemStatus.NOT_MET,
        0,
        0,
    )


# --- 자격증·학위 -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "related", "expected"),
    [
        ("전기산업기사", True, (ItemStatus.MET, 20, 20)),
        ("용접 기능사", True, (ItemStatus.MET, 20, 20)),
        ("용접기능사", False, (ItemStatus.NOT_MET, 0, 0)),
        ("용접기능사", None, (ItemStatus.NEEDS_EVIDENCE, None, None)),
        ("컴퓨터활용능력", True, (ItemStatus.NOT_MET, 0, 0)),
        ("용접기능장", True, (ItemStatus.NEEDS_REVIEW, 0, 20)),
    ],
)
def test_certificates(
    name: str, related: bool | None, expected: tuple[ItemStatus, int | None, int | None]
) -> None:
    ev = _ev(1, "NATIONAL_CERTIFICATE", levelValue=name, relatedToCurrentJob=related)
    assert _scores(calculate_qualification_bonus(_profile(evidences=[ev]), _rules())) == expected


def test_degree_needs_no_job_relation_and_counts_once_with_certificate() -> None:
    evidences = [
        _ev(1, "KOREAN_DEGREE", levelValue="전문학사"),
        _ev(2, "NATIONAL_CERTIFICATE", levelValue="용접기능사", relatedToCurrentJob=True),
    ]
    item = calculate_qualification_bonus(_profile(evidences=evidences), _rules())
    assert _scores(item) == (ItemStatus.MET, 20, 20)


def test_qualification_asks_for_missing_name() -> None:
    item = calculate_qualification_bonus(
        _profile(evidences=[_ev(3, "NATIONAL_CERTIFICATE")]), _rules()
    )
    assert item is not None
    assert [m.field for m in item.missing_inputs] == ["visaEvidences[visaEvidenceId=3].levelValue"]


# --- 운전면허 --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("license_class", "expected"),
    [
        ("제2종 보통면허", (ItemStatus.MET, 10, 10)),
        ("1종대형", (ItemStatus.MET, 10, 10)),
        ("2종 소형", (ItemStatus.NOT_MET, 0, 0)),
        (None, (ItemStatus.NEEDS_EVIDENCE, None, None)),
    ],
)
def test_driver_license_classes(
    license_class: str | None, expected: tuple[ItemStatus, int | None, int | None]
) -> None:
    ev = _ev(1, "DRIVER_LICENSE", levelValue=license_class)
    assert _scores(calculate_license_bonus(_profile(evidences=[ev]), _rules())) == expected


# --- 감점 -----------------------------------------------------------------------------

CLEAN = {
    "finesUnder1mCount": 0,
    "hasFine1mOrMore": False,
    "taxStayRestrictionCount": 0,
    "immigrationViolationCount": 0,
    "hasOverstay3mOrMore": False,
    "hasCurrentTaxArrears": False,
}


@pytest.mark.parametrize(("count", "points"), [(0, 0), (1, -5), (2, -10), (3, -20), (5, -20)])
def test_fine_penalty_schedule(count: int, points: int) -> None:
    items = _by_code(
        calculate_penalties(_profile(declaration={**CLEAN, "finesUnder1mCount": count}), _rules())
    )
    assert items["PEN_FINE_UNDER_1M"].earned_points == points


def test_maximum_penalties_total_minus_50() -> None:
    declaration = {
        **CLEAN,
        "finesUnder1mCount": 3,
        "taxStayRestrictionCount": 3,
        "immigrationViolationCount": 3,
    }
    items = calculate_penalties(_profile(declaration=declaration), _rules())
    assert sum(item.earned_points or 0 for item in items) == -50
    assert all(item.status is ItemStatus.MET for item in items)


def test_four_immigration_violations_are_an_exclusion_not_a_penalty() -> None:
    items = _by_code(
        calculate_penalties(
            _profile(declaration={**CLEAN, "immigrationViolationCount": 4}), _rules()
        )
    )
    assert _scores(items["PEN_IMMIGRATION"]) == (ItemStatus.NOT_APPLICABLE, 0, 0)


def test_undeclared_penalties_are_asked_not_zeroed() -> None:
    items = calculate_penalties(_profile(), _rules())
    assert [_scores(item) for item in items] == [(ItemStatus.NEEDS_EVIDENCE, None, None)] * 3
    assert [item.missing_inputs[0].field for item in items] == [
        "legalDeclaration.finesUnder1mCount",
        "legalDeclaration.taxStayRestrictionCount",
        "legalDeclaration.immigrationViolationCount",
    ]


def test_partially_declared_penalties() -> None:
    items = _by_code(
        calculate_penalties(
            _profile(declaration={**CLEAN, "taxStayRestrictionCount": None}), _rules()
        )
    )
    assert items["PEN_TAX_RESTRICTION"].status is ItemStatus.NEEDS_EVIDENCE
    assert items["PEN_FINE_UNDER_1M"].status is ItemStatus.NOT_MET


# --- 전체 ------------------------------------------------------------------------------


def test_bonus_items_follow_rule_order_one_per_rule() -> None:
    items = calculate_bonus_items(_profile([_job(1, "2020-01-01")]), _rules(), AS_OF)
    assert [item.rule_code for item in items] == [
        "REC_CENTRAL",
        "REC_REGIONAL",
        "REC_EMPLOYER",
        "TENURE_3Y",
        "REGION_3Y",
        "QUALIFICATION",
        "DRIVER_LICENSE",
    ]

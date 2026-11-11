from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.visa.e74.rules import (
    RULE_SETS_DIR,
    AgeRange,
    ApprovalStatus,
    Contract,
    DeclaredCountAtLeast,
    DeclaredCountPenalty,
    EvidencePresent,
    IncomeRange,
    KoreanLevel,
    MinAverageIncome,
    MinKorean,
    RuleCategory,
    RuleSet,
    RuleSetError,
    StayPeriod,
    default_rule_sets,
    load_rule_sets,
    select_rule_set,
)

pytestmark = pytest.mark.unit

V2026 = "E74-2026-v1"
AGRI_FISHERY = ("AGRICULTURE", "LIVESTOCK", "FISHERY", "COASTAL_SHIPPING")


def _rule_set() -> RuleSet:
    return next(rs for rs in default_rule_sets() if rs.rule_version == V2026)


def _raw() -> dict[str, Any]:
    raw: dict[str, Any] = json.loads((RULE_SETS_DIR / f"{V2026}.json").read_text(encoding="utf-8"))
    return raw


# --- 공식 숫자 대조: 2026 선발계획 붙임1 점수표(p12)와 본문(p2~p5) ---------------


def test_2026_rule_set_header() -> None:
    rs = _rule_set()
    assert rs.effective_from == date(2026, 3, 1)
    assert rs.effective_to == date(2027, 1, 1)
    assert rs.required_total_score == 200
    assert rs.source_policy_version == "E74-SELECTION-2026-20260601"
    # 해석 합의 전이라 DRAFT. 팀 확인 후 APPROVED로 바꾼다.
    assert rs.approval_status is ApprovalStatus.DRAFT


INCOME_TABLE = {
    "INCOME_25_30": (25_000_000, 30_000_000, 50),
    "INCOME_30_35": (30_000_000, 35_000_000, 65),
    "INCOME_35_40": (35_000_000, 40_000_000, 80),
    "INCOME_40_45": (40_000_000, 45_000_000, 95),
    "INCOME_45_50": (45_000_000, 50_000_000, 110),
    "INCOME_50_PLUS": (50_000_000, None, 120),
}


@pytest.mark.parametrize("code", INCOME_TABLE)
def test_income_bands_match_official_table(code: str) -> None:
    rule = _rule_set().rule(code)
    assert isinstance(rule.condition, IncomeRange)
    expected_min, expected_max, expected_points = INCOME_TABLE[code]
    assert (rule.condition.min_amount, rule.condition.max_amount) == (expected_min, expected_max)
    assert rule.points == expected_points
    assert rule.condition.industry_codes is None


def test_general_income_bands_are_contiguous_from_25m() -> None:
    bands = sorted(
        (r.condition.min_amount, r.condition.max_amount)
        for r in _rule_set().rules_in(RuleCategory.BASE_INCOME)
        if isinstance(r.condition, IncomeRange) and r.industry_type is None
    )
    assert bands[0][0] == 25_000_000 and bands[-1][1] is None
    for (_, upper), (lower, _) in zip(bands, bands[1:], strict=False):
        assert upper == lower


def test_agri_fishery_income_band_is_flagged_for_review() -> None:
    rule = _rule_set().rule("INCOME_24_25_AGRI_FISHERY")
    assert isinstance(rule.condition, IncomeRange)
    assert (rule.condition.min_amount, rule.condition.max_amount) == (24_000_000, 25_000_000)
    assert rule.condition.industry_codes == AGRI_FISHERY
    assert rule.review_note is not None and rule.review_note.startswith("Q1")


KOREAN_TABLE = {
    # (TOPIK 최소·최대, KIIP 단계 최소·최대, 사전평가 최소·최대, 점수)
    "KOREAN_L2": ((2, 2), (2, 2), (41, 60), 50),
    "KOREAN_L3": ((3, 3), (3, 3), (61, 80), 80),
    "KOREAN_L4_PLUS": ((4, None), (4, None), (81, None), 120),
}


@pytest.mark.parametrize("code", KOREAN_TABLE)
def test_korean_levels_match_official_table(code: str) -> None:
    rule = _rule_set().rule(code)
    c = rule.condition
    assert isinstance(c, KoreanLevel)
    topik, kiip, pre_test, points = KOREAN_TABLE[code]
    assert (c.topik_level_min, c.topik_level_max) == topik
    assert (c.kiip_stage_min, c.kiip_stage_max) == kiip
    assert (c.pre_test_score_min, c.pre_test_score_max) == pre_test
    assert rule.points == points
    # 사전평가는 결과일로부터 2년 유효, 3단계 이상 배정돼야 인정(p3, p8).
    assert c.pre_test_validity_months == 24 and c.pre_test_min_assigned_stage == 3


AGE_TABLE = {
    "AGE_19_26": (19, 26, 40),
    "AGE_27_33": (27, 33, 60),
    "AGE_34_40": (34, 40, 30),
    "AGE_41_PLUS": (41, None, 10),
}


@pytest.mark.parametrize("code", AGE_TABLE)
def test_age_bands_match_official_table(code: str) -> None:
    rule = _rule_set().rule(code)
    assert isinstance(rule.condition, AgeRange)
    assert (rule.condition.min_age, rule.condition.max_age, rule.points) == AGE_TABLE[code]


def test_base_items_total_300() -> None:
    rs = _rule_set()
    top = {
        category: max(r.points or 0 for r in rs.rules_in(category))
        for category in (RuleCategory.BASE_INCOME, RuleCategory.BASE_KOREAN, RuleCategory.BASE_AGE)
    }
    assert top == {
        RuleCategory.BASE_INCOME: 120,
        RuleCategory.BASE_KOREAN: 120,
        RuleCategory.BASE_AGE: 60,
    }
    assert sum(top.values()) == 300


BONUS_TABLE = {
    "REC_CENTRAL": 30,
    "REC_REGIONAL": 30,
    "REC_EMPLOYER": 50,
    "TENURE_3Y": 20,
    "REGION_3Y": 20,
    "QUALIFICATION": 20,
    "DRIVER_LICENSE": 10,
}


def test_bonus_points_match_official_table() -> None:
    rs = _rule_set()
    assert {r.rule_code: r.points for r in rs.rules_in(RuleCategory.BONUS)} == BONUS_TABLE


def test_central_and_regional_recommendations_share_one_group() -> None:
    rs = _rule_set()
    groups = {r.rule_code: r.exclusive_group for r in rs.rules_in(RuleCategory.BONUS)}
    assert groups["REC_CENTRAL"] == groups["REC_REGIONAL"] == "REC_GOV"
    assert [code for code, group in groups.items() if group == "REC_GOV"] == [
        "REC_CENTRAL",
        "REC_REGIONAL",
    ]
    employer = rs.rule("REC_EMPLOYER").condition
    assert isinstance(employer, EvidencePresent)
    assert employer.evidence_type == "RECOMMENDATION_EMPLOYER"


PENALTY_TABLE = {
    "PEN_FINE_UNDER_1M": ("finesUnder1mCount", (5, 10, 20)),
    "PEN_TAX_RESTRICTION": ("taxStayRestrictionCount", (5, 10, 15)),
    "PEN_IMMIGRATION": ("immigrationViolationCount", (5, 10, 15)),
}


@pytest.mark.parametrize("code", PENALTY_TABLE)
def test_penalties_match_official_table(code: str) -> None:
    rule = _rule_set().rule(code)
    assert isinstance(rule.condition, DeclaredCountPenalty)
    field, schedule = PENALTY_TABLE[code]
    assert rule.condition.declaration_field == field
    assert rule.condition.points_by_count == schedule
    assert rule.points == -schedule[-1]


def test_penalty_caps_add_up_to_50() -> None:
    assert sum(-(r.points or 0) for r in _rule_set().rules_in(RuleCategory.PENALTY)) == 50


def test_exclusions_match_official_list() -> None:
    rs = _rule_set()
    kinds = {r.rule_code: r.condition.kind for r in rs.rules_in(RuleCategory.EXCLUSION)}
    assert kinds == {
        "EXC_FINE_1M_PLUS": "DECLARED_FLAG",
        "EXC_TAX_ARREARS": "DECLARED_FLAG",
        "EXC_IMMIGRATION_4PLUS": "DECLARED_COUNT_AT_LEAST",
        "EXC_OVERSTAY_3M": "DECLARED_FLAG",
        "EXC_PUBLIC_SAFETY": "MANUAL_REVIEW",
        "EXC_SOCIAL_ORDER": "MANUAL_REVIEW",
    }
    immigration = rs.rule("EXC_IMMIGRATION_4PLUS").condition
    assert isinstance(immigration, DeclaredCountAtLeast) and immigration.min_count == 4


def test_requirements_match_official_thresholds() -> None:
    rs = _rule_set()
    stay = rs.rule("REQ_STAY_PERIOD").condition
    assert isinstance(stay, StayPeriod)
    assert (stay.min_months, stay.lookback_months, stay.non_capital_special_min_months) == (
        48,
        120,
        36,
    )
    assert stay.visa_types == ("E-9", "E-10", "H-2")
    assert stay.capital_area_sido_codes == ("11", "28", "41")  # 서울·인천·경기

    contract = rs.rule("REQ_CONTRACT").condition
    special_contract = rs.rule("REQ_CONTRACT_AGRI_FISHERY").condition
    assert isinstance(contract, Contract) and isinstance(special_contract, Contract)
    assert (contract.min_annual_salary, contract.min_contract_months) == (26_000_000, 24)
    assert (special_contract.min_annual_salary, special_contract.industry_codes) == (
        25_000_000,
        AGRI_FISHERY,
    )

    income = rs.rule("REQ_MIN_INCOME").condition
    special_income = rs.rule("REQ_MIN_INCOME_AGRI_FISHERY").condition
    assert isinstance(income, MinAverageIncome) and isinstance(special_income, MinAverageIncome)
    assert income.min_amount == 25_000_000 and income.income_years == 2
    assert (income.fallback_year_from, income.fallback_year_to) == (2021, 2025)
    assert special_income.min_amount == 24_000_000

    korean = rs.rule("REQ_MIN_KOREAN").condition
    assert isinstance(korean, MinKorean)
    assert (korean.min_points, korean.deferral_until, korean.deferral_min_total_without_korean) == (
        50,
        date(2026, 12, 31),
        150,
    )


# --- 버전 선택 -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("as_of", "expected"),
    [
        (date(2026, 2, 28), None),
        (date(2026, 3, 1), V2026),
        (date(2026, 12, 31), V2026),
        (date(2027, 1, 1), None),
    ],
)
def test_selection_uses_effective_period_boundaries(as_of: date, expected: str | None) -> None:
    selected = select_rule_set(as_of, default_rule_sets(), include_draft=True)
    assert (selected.rule_version if selected else None) == expected


def test_draft_is_not_selected_unless_allowed() -> None:
    assert select_rule_set(date(2026, 10, 25), default_rule_sets()) is None


def test_approved_wins_over_draft() -> None:
    draft = _rule_set()
    approved = draft.model_copy(
        update={"rule_version": "E74-2026-v2", "approval_status": ApprovalStatus.APPROVED}
    )
    selected = select_rule_set(date(2026, 10, 25), [draft, approved], include_draft=True)
    assert selected is approved


def test_retired_is_never_selected() -> None:
    retired = _rule_set().model_copy(update={"approval_status": ApprovalStatus.RETIRED})
    assert select_rule_set(date(2026, 10, 25), [retired], include_draft=True) is None


# --- 잘못된 규칙 데이터 거부 ---------------------------------------------------------


def _rule_index(raw: dict[str, Any], code: str) -> int:
    return next(i for i, r in enumerate(raw["rules"]) if r["ruleCode"] == code)


def _broken(edit: str) -> dict[str, Any]:
    raw = _raw()
    rules = raw["rules"]
    if edit == "duplicate_rule_code":
        rules.append(dict(rules[0]))
    elif edit == "kind_not_allowed_for_category":
        rules[_rule_index(raw, "INCOME_25_30")]["category"] = "BONUS"
    elif edit == "positive_penalty_points":
        rules[_rule_index(raw, "PEN_FINE_UNDER_1M")]["points"] = 20
    elif edit == "penalty_points_not_cap":
        rules[_rule_index(raw, "PEN_FINE_UNDER_1M")]["points"] = -15
    elif edit == "points_on_requirement":
        rules[_rule_index(raw, "REQ_CONTRACT")]["points"] = 10
    elif edit == "missing_points_on_bonus":
        rules[_rule_index(raw, "DRIVER_LICENSE")]["points"] = None
    elif edit == "industry_type_without_codes":
        rules[_rule_index(raw, "REQ_CONTRACT")]["industryType"] = "AGRI_FISHERY"
    elif edit == "income_band_reversed":
        rules[_rule_index(raw, "INCOME_25_30")]["condition"]["maxAmount"] = 20_000_000
    elif edit == "rising_schedule_broken":
        rules[_rule_index(raw, "PEN_FINE_UNDER_1M")]["condition"]["pointsByCount"] = [10, 5, 20]
    elif edit == "special_income_row_with_basis":
        rules[_rule_index(raw, "REQ_MIN_INCOME_AGRI_FISHERY")]["condition"]["incomeYears"] = 2
    elif edit == "unknown_kind":
        rules[0]["condition"]["kind"] = "LLM_GUESS"
    elif edit == "effective_to_before_from":
        raw["effectiveTo"] = "2026-02-01"
    elif edit == "unknown_field":
        raw["approvedBy"] = "someone"
    return raw


@pytest.mark.parametrize(
    "edit",
    [
        "duplicate_rule_code",
        "kind_not_allowed_for_category",
        "positive_penalty_points",
        "penalty_points_not_cap",
        "points_on_requirement",
        "missing_points_on_bonus",
        "industry_type_without_codes",
        "income_band_reversed",
        "rising_schedule_broken",
        "special_income_row_with_basis",
        "unknown_kind",
        "effective_to_before_from",
        "unknown_field",
    ],
)
def test_invalid_rule_data_is_rejected(edit: str) -> None:
    with pytest.raises(ValidationError):
        RuleSet.model_validate(_broken(edit))


def _write(directory: Path, name: str, raw: dict[str, Any]) -> None:
    (directory / name).write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")


def test_file_name_must_match_version(tmp_path: Path) -> None:
    _write(tmp_path, "E74-2026-v9.json", _raw())
    with pytest.raises(RuleSetError):
        load_rule_sets(tmp_path)


def test_overlapping_approved_versions_are_rejected(tmp_path: Path) -> None:
    for version in ("E74-2026-v1", "E74-2026-v2"):
        raw = dict(_raw(), ruleVersion=version, approvalStatus="APPROVED")
        _write(tmp_path, f"{version}.json", raw)
    with pytest.raises(RuleSetError):
        load_rule_sets(tmp_path)


def test_back_to_back_versions_load(tmp_path: Path) -> None:
    first = dict(_raw(), approvalStatus="APPROVED")
    second = dict(
        _raw(),
        ruleVersion="E74-2027-v1",
        effectiveFrom="2027-01-01",
        effectiveTo=None,
        approvalStatus="APPROVED",
    )
    _write(tmp_path, "E74-2026-v1.json", first)
    _write(tmp_path, "E74-2027-v1.json", second)
    loaded = load_rule_sets(tmp_path)
    assert [rs.rule_version for rs in loaded] == ["E74-2026-v1", "E74-2027-v1"]
    assert _selected_version(date(2026, 12, 31), loaded) == "E74-2026-v1"
    assert _selected_version(date(2027, 1, 1), loaded) == "E74-2027-v1"


def _selected_version(as_of: date, rule_sets: tuple[RuleSet, ...]) -> str | None:
    selected = select_rule_set(as_of, rule_sets)
    return selected.rule_version if selected else None

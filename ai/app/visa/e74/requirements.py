"""필수요건·제외 판정 (명세 3.4, 정의서 5·6장).

요건과 제외는 점수가 없다(earnedPoints·potentialPoints = null).
- 요건: 확인된 값으로 충족 → MET, 신고값으로만 충족 → NEEDS_EVIDENCE, 값이 없으면 재질의,
  해석이 필요하면 NEEDS_REVIEW, 확인된 미달 → NOT_MET.
- 제외: 해당하면 MET(신청 불가), 아니면 NOT_MET, 신고가 없으면 재질의, 재량 사유는 NOT_APPLICABLE.
- 업종 특례 행이 있는 요건은 적용되는 행만 판정하고 다른 행은 NOT_APPLICABLE이다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from app.visa.e74.dates import add_months, full_months_between
from app.visa.e74.employment import current_stint, left_without_fault_missing, stint_coverage
from app.visa.e74.questions import declaration_missing, declared_value
from app.visa.e74.results import ItemResult, ItemStatus, MissingInput
from app.visa.e74.rules import (
    Contract,
    CurrentEmployment,
    DeclaredCountAtLeast,
    DeclaredFlag,
    EmployerRecommendation,
    ManualReview,
    MinAverageIncome,
    MinKorean,
    Rule,
    RuleCategory,
    RuleSet,
    StayPeriod,
    TotalScore,
)
from app.visa.e74.schemas import (
    E74Profile,
    EvidenceType,
    ProfileEvidenceStatus,
    VerificationStatus,
)

REQUIREMENT = RuleCategory.REQUIREMENT
EXCLUSION = RuleCategory.EXCLUSION
KOREAN_DEFERRED = "E74_KOREAN_DEFERRED"

EMPLOYMENT_MISSING = MissingInput(
    field="employmentHistories", question="근무 이력(사업장, 업종, 근무 시작일·종료일)"
)


@dataclass(frozen=True)
class ScoreItems:
    """요건 판정에 쓰는 점수 항목들(4·5단계 결과)."""

    income: ItemResult
    korean: ItemResult
    age: ItemResult
    bonuses: tuple[ItemResult, ...]
    penalties: tuple[ItemResult, ...]

    def all(self) -> tuple[ItemResult, ...]:
        return (self.income, self.korean, self.age, *self.bonuses, *self.penalties)


def _item(
    rule: Rule,
    status: ItemStatus,
    message_code: str,
    evidence: dict[str, Any] | None = None,
    *,
    missing: Sequence[MissingInput] = (),
    review_note: str | None = None,
) -> ItemResult:
    return ItemResult(
        rule_code=rule.rule_code,
        category=rule.category,
        status=status,
        earned_points=None,
        potential_points=None,
        message_code=message_code,
        evidence=evidence or {},
        missing_inputs=tuple(missing),
        review_note=review_note,
    )


def _met(rule: Rule, verified: bool, message_code: str, evidence: dict[str, Any]) -> ItemResult:
    status = ItemStatus.MET if verified else ItemStatus.NEEDS_EVIDENCE
    return _item(rule, status, message_code, evidence)


def _not_applicable(rule: Rule, applied: Rule) -> ItemResult:
    return _item(
        rule,
        ItemStatus.NOT_APPLICABLE,
        "E74_REQ_OTHER_INDUSTRY_ROW",
        {"appliedRuleCode": applied.rule_code},
    )


def _industry_split(
    rule_set: RuleSet, kind: type, industry: str | None
) -> tuple[Rule, list[Rule]] | None:
    """같은 kind의 일반 행과 특례 행 중 적용 행 하나와 나머지를 고른다."""
    rules = [r for r in rule_set.rules_in(REQUIREMENT) if isinstance(r.condition, kind)]
    if not rules:
        return None
    special = [
        r
        for r in rules
        if (codes := getattr(r.condition, "industry_codes", None)) is not None and industry in codes
    ]
    general = [r for r in rules if getattr(r.condition, "industry_codes", None) is None]
    applied = special[0] if special else general[0] if general else rules[0]
    return applied, [r for r in rules if r is not applied]


def _profile_verified(profile: E74Profile) -> bool:
    return profile.visa_profile.evidence_status is ProfileEvidenceStatus.VERIFIED


# --- 체류 ----------------------------------------------------------------------------


def _stay_period(rule: Rule, c: StayPeriod, profile: E74Profile, as_of: date) -> ItemResult:
    vp = profile.visa_profile
    if vp.current_stay_started_on is None:
        return _item(
            rule,
            ItemStatus.NEEDS_EVIDENCE,
            "E74_REQ_STAY_MISSING",
            missing=[
                MissingInput(
                    field="visaProfile.currentStayStartedOn",
                    question="현재 체류를 시작한 날(이번에 한국에 입국한 날)",
                )
            ],
        )
    window_start = add_months(as_of, -c.lookback_months)
    current_months = full_months_between(max(vp.current_stay_started_on, window_start), as_of)
    evidence: dict[str, Any] = {"currentStayMonths": current_months, "requiredMonths": c.min_months}
    verified = _profile_verified(profile)
    if current_months >= c.min_months:
        return _met(rule, verified, "E74_REQ_STAY_PERIOD", evidence)
    if vp.prior_stay_months is not None:
        total = current_months + vp.prior_stay_months
        evidence |= {"priorStayMonths": vp.prior_stay_months, "totalMonths": total}
        if total >= c.min_months:
            return _met(rule, verified, "E74_REQ_STAY_PERIOD", evidence)
    if current_months >= c.non_capital_special_min_months and _non_capital_with_province_rec(
        c, profile
    ):
        return _item(
            rule,
            ItemStatus.NEEDS_REVIEW,
            "E74_REQ_STAY_NON_CAPITAL_SPECIAL",
            evidence,
            review_note="Q8: 비수도권 3년 체류 특례(광역지자체 추천) 적용 여부 확인 필요",
        )
    if vp.prior_stay_months is None:
        return _item(
            rule,
            ItemStatus.NEEDS_EVIDENCE,
            "E74_REQ_STAY_PRIOR_MISSING",
            evidence,
            missing=[
                MissingInput(
                    field="visaProfile.priorStayMonths",
                    question=(
                        "이번 입국 전, 최근 10년 안에 E-9·E-10·H-2로 한국에 있었던 기간 합계(개월)"
                    ),
                )
            ],
        )
    return _item(rule, ItemStatus.NOT_MET, "E74_REQ_STAY_SHORT", evidence)


def _non_capital_with_province_rec(c: StayPeriod, profile: E74Profile) -> bool:
    current = profile.current_employment
    region = current.workplace_region_code if current is not None else None
    if region is None or region[:2] in c.capital_area_sido_codes:
        return False
    return any(
        ev.evidence_type is EvidenceType.RECOMMENDATION_PROVINCE
        and ev.status is not VerificationStatus.REJECTED
        for ev in profile.visa_evidences
    )


# --- 현재 근무, 고용계약, 기업 추천 ------------------------------------------------------


def _current_employment(rule: Rule, c: CurrentEmployment, profile: E74Profile) -> ItemResult:
    visa = profile.visa_profile.visa_type_code
    if visa is None:
        return _item(
            rule,
            ItemStatus.NEEDS_EVIDENCE,
            "E74_REQ_VISA_MISSING",
            missing=[
                MissingInput(field="visaProfile.visaTypeCode", question="현재 체류자격(예: E-9)")
            ],
        )
    evidence: dict[str, Any] = {"visaTypeCode": visa}
    if visa not in c.visa_types:
        return _item(rule, ItemStatus.NOT_MET, "E74_REQ_VISA_TYPE", evidence)
    if not profile.employment_histories:
        return _item(
            rule,
            ItemStatus.NEEDS_EVIDENCE,
            "E74_EMPLOYMENT_MISSING",
            evidence,
            missing=[EMPLOYMENT_MISSING],
        )
    current = profile.current_employment
    if current is None:
        return _item(rule, ItemStatus.NOT_MET, "E74_REQ_NO_CURRENT_JOB", evidence)
    evidence["employmentHistoryId"] = current.employment_history_id
    verified = (
        _profile_verified(profile) and current.verification_status is VerificationStatus.VERIFIED
    )
    return _met(rule, verified, "E74_REQ_CURRENT_EMPLOYMENT", evidence)


def _contract(rule: Rule, c: Contract, profile: E74Profile, as_of: date) -> ItemResult:
    if not profile.employment_histories:
        return _item(
            rule, ItemStatus.NEEDS_EVIDENCE, "E74_EMPLOYMENT_MISSING", missing=[EMPLOYMENT_MISSING]
        )
    current = profile.current_employment
    if current is None:
        return _item(rule, ItemStatus.NOT_MET, "E74_REQ_NO_CURRENT_JOB")
    prefix = f"employmentHistories[employmentHistoryId={current.employment_history_id}]"
    missing: list[MissingInput] = []
    if current.contract_end_on is None:
        missing.append(
            MissingInput(
                field=f"{prefix}.contractEndOn",
                question="현재 회사와 맺은(맺을) E-7-4 고용계약의 종료일",
            )
        )
    if current.contract_annual_salary is None:
        missing.append(
            MissingInput(
                field=f"{prefix}.contractAnnualSalary",
                question="E-7-4 고용계약 연봉(기본급·수당을 포함한 1년 총액)",
            )
        )
    if missing or current.contract_end_on is None or current.contract_annual_salary is None:
        return _item(rule, ItemStatus.NEEDS_EVIDENCE, "E74_REQ_CONTRACT_MISSING", missing=missing)

    # 기준일부터 minContractMonths개월째 되는 날의 전날까지 근무하면 그 기간을 채운다.
    last_required_day = add_months(as_of, c.min_contract_months) - timedelta(days=1)
    period_ok = current.contract_end_on >= last_required_day
    salary_ok = current.contract_annual_salary >= c.min_annual_salary
    evidence: dict[str, Any] = {
        "employmentHistoryId": current.employment_history_id,
        "contractEndOn": current.contract_end_on.isoformat(),
        "requiredEndOn": last_required_day.isoformat(),
        "annualSalary": current.contract_annual_salary,
        "requiredAnnualSalary": c.min_annual_salary,
    }
    if not (period_ok and salary_ok):
        message = "E74_REQ_CONTRACT_SALARY_LOW" if period_ok else "E74_REQ_CONTRACT_TOO_SHORT"
        return _item(rule, ItemStatus.NOT_MET, message, evidence)
    verified = current.verification_status is VerificationStatus.VERIFIED
    return _met(rule, verified, "E74_REQ_CONTRACT", evidence)


def _employer_recommendation(
    rule: Rule, c: EmployerRecommendation, profile: E74Profile, as_of: date
) -> ItemResult:
    if not profile.employment_histories:
        return _item(
            rule, ItemStatus.NEEDS_EVIDENCE, "E74_EMPLOYMENT_MISSING", missing=[EMPLOYMENT_MISSING]
        )
    stint = current_stint(profile)
    if stint is None:
        return _item(rule, ItemStatus.NOT_MET, "E74_REQ_NO_CURRENT_JOB")
    coverage = stint_coverage(stint, as_of, c.min_tenure_months)
    evidence: dict[str, Any] = {"coveredDays": coverage.days, "requiredDays": coverage.required}
    if not coverage.met:
        if coverage.unknown_previous is not None:
            return _item(
                rule,
                ItemStatus.NEEDS_EVIDENCE,
                "E74_REQ_EMPLOYER_TENURE_REASON_MISSING",
                evidence,
                missing=[left_without_fault_missing(coverage.unknown_previous)],
            )
        return _item(rule, ItemStatus.NOT_MET, "E74_REQ_EMPLOYER_TENURE_SHORT", evidence)
    recs = [
        ev
        for ev in profile.visa_evidences
        if ev.evidence_type is c.evidence_type and ev.status is not VerificationStatus.REJECTED
    ]
    if not recs:
        return _item(
            rule,
            ItemStatus.NEEDS_EVIDENCE,
            "E74_REQ_EMPLOYER_REC_MISSING",
            evidence,
            missing=[
                MissingInput(
                    field=f"visaEvidences[evidenceType={c.evidence_type.value}]",
                    question="현재 회사 대표의 고용기업 추천서를 받았는지(받을 수 있는지)",
                )
            ],
        )
    evidence["visaEvidenceIds"] = [ev.visa_evidence_id for ev in recs]
    verified = any(ev.status is VerificationStatus.VERIFIED for ev in recs) and all(
        row.verification_status is VerificationStatus.VERIFIED for row in coverage.rows
    )
    return _met(rule, verified, "E74_REQ_EMPLOYER_REC", evidence)


# --- 소득, 한국어, 총점 ----------------------------------------------------------------


def _min_income(rule: Rule, c: MinAverageIncome, scores: ScoreItems) -> ItemResult:
    income = scores.income
    average = income.evidence.get("averageIncome")
    if not isinstance(average, int):
        return _item(
            rule,
            ItemStatus.NEEDS_EVIDENCE,
            "E74_REQ_INCOME_MISSING",
            missing=income.missing_inputs,
        )
    evidence: dict[str, Any] = {"averageIncome": average, "requiredAverageIncome": c.min_amount}
    if average < c.min_amount:
        return _item(rule, ItemStatus.NOT_MET, "E74_REQ_INCOME_LOW", evidence)
    if income.status is ItemStatus.NEEDS_REVIEW:
        return _item(
            rule,
            ItemStatus.NEEDS_REVIEW,
            "E74_REQ_INCOME_REVIEW",
            evidence,
            review_note=income.review_note,
        )
    return _met(rule, income.status is ItemStatus.MET, "E74_REQ_INCOME", evidence)


@dataclass(frozen=True)
class Totals:
    """점수 항목 합계: 확정 총점과 가능 총점."""

    confirmed: int
    potential: int
    # 모르는 항목(가능 점수 null)이 없는지. 감점을 모르면 확정 총점도 확정할 수 없다.
    complete: bool
    penalties_known: bool


def totals(items: Sequence[ItemResult]) -> Totals:
    return Totals(
        confirmed=sum(item.earned_points or 0 for item in items),
        potential=sum(item.potential_points or 0 for item in items),
        complete=all(item.potential_points is not None for item in items),
        penalties_known=all(
            item.earned_points is not None
            for item in items
            if item.category is RuleCategory.PENALTY
        ),
    )


def _min_korean(rule: Rule, c: MinKorean, scores: ScoreItems, as_of: date) -> ItemResult:
    korean = scores.korean
    evidence: dict[str, Any] = {
        "koreanPoints": korean.potential_points,
        "requiredPoints": c.min_points,
    }
    if (korean.potential_points or 0) >= c.min_points:
        if korean.status is ItemStatus.NEEDS_REVIEW:
            return _item(
                rule,
                ItemStatus.NEEDS_REVIEW,
                "E74_REQ_KOREAN_REVIEW",
                evidence,
                review_note=korean.review_note,
            )
        verified = (korean.earned_points or 0) >= c.min_points
        return _met(rule, verified, "E74_REQ_KOREAN", evidence)

    deferral = _korean_deferral(c, scores, as_of)
    if deferral is not None:
        evidence |= deferral
        verified = bool(deferral["deferralConfirmed"])
        return _item(
            rule,
            ItemStatus.MET if verified else ItemStatus.NEEDS_EVIDENCE,
            KOREAN_DEFERRED,
            evidence,
        )
    if korean.missing_inputs:
        return _item(
            rule,
            ItemStatus.NEEDS_EVIDENCE,
            "E74_REQ_KOREAN_MISSING",
            evidence,
            missing=korean.missing_inputs,
        )
    return _item(rule, ItemStatus.NOT_MET, "E74_REQ_KOREAN_LOW", evidence)


def _korean_deferral(c: MinKorean, scores: ScoreItems, as_of: date) -> dict[str, Any] | None:
    """한국어 한시 유예: 기간 안이고 한국어 외 총점·소득 기준을 채우면 유예(정의서 6장)."""
    if c.deferral_until is None or c.deferral_min_total_without_korean is None:
        return None
    if as_of > c.deferral_until:
        return None
    without_korean = [item for item in scores.all() if item is not scores.korean]
    t = totals(without_korean)
    need = c.deferral_min_total_without_korean
    income_confirmed = (scores.income.earned_points or 0) > 0
    income_possible = (scores.income.potential_points or 0) > 0
    confirmed = t.penalties_known and income_confirmed and t.confirmed >= need
    if not confirmed and not (income_possible and t.potential >= need):
        return None
    return {
        "deferralUntil": c.deferral_until.isoformat(),
        "totalWithoutKorean": t.confirmed,
        "potentialTotalWithoutKorean": t.potential,
        "requiredTotalWithoutKorean": need,
        "deferralConfirmed": confirmed,
    }


def _total_score(
    rule: Rule, rule_set: RuleSet, scores: ScoreItems, deferral_threshold: int | None
) -> ItemResult:
    required = rule_set.required_total_score
    if required is None:
        return _item(
            rule,
            ItemStatus.NEEDS_REVIEW,
            "E74_REQ_TOTAL_UNKNOWN",
            review_note="규칙 버전에 기준 총점이 없음(공식 확인 전)",
        )
    threshold = deferral_threshold if deferral_threshold is not None else required
    t = totals(scores.all())
    evidence: dict[str, Any] = {
        "confirmedTotal": t.confirmed,
        "potentialTotal": t.potential,
        "requiredTotal": threshold,
        "complete": t.complete,
        "koreanDeferral": deferral_threshold is not None,
    }
    if t.penalties_known and t.confirmed >= threshold:
        return _item(rule, ItemStatus.MET, "E74_REQ_TOTAL", evidence)
    if t.potential >= threshold or not t.complete:
        return _item(rule, ItemStatus.NEEDS_EVIDENCE, "E74_REQ_TOTAL_UNCONFIRMED", evidence)
    return _item(rule, ItemStatus.NOT_MET, "E74_REQ_TOTAL_LOW", evidence)


# --- 진입점 ----------------------------------------------------------------------------


def calculate_requirement_items(
    profile: E74Profile, rule_set: RuleSet, as_of: date, scores: ScoreItems
) -> list[ItemResult]:
    """필수요건 항목(규칙 순서). 업종 특례가 있는 요건은 적용 행만 판정한다."""
    current = profile.current_employment
    industry = current.industry_code if current is not None else None
    results: dict[str, ItemResult] = {}

    for kind in (Contract, MinAverageIncome):
        split = _industry_split(rule_set, kind, industry)
        if split is None:
            continue
        applied, others = split
        condition = applied.condition
        if isinstance(condition, Contract):
            results[applied.rule_code] = _contract(applied, condition, profile, as_of)
        elif isinstance(condition, MinAverageIncome):
            results[applied.rule_code] = _min_income(applied, condition, scores)
        for other in others:
            results[other.rule_code] = _not_applicable(other, applied)

    deferral_threshold: int | None = None
    for rule in rule_set.rules_in(REQUIREMENT):
        c = rule.condition
        if isinstance(c, StayPeriod):
            results[rule.rule_code] = _stay_period(rule, c, profile, as_of)
        elif isinstance(c, CurrentEmployment):
            results[rule.rule_code] = _current_employment(rule, c, profile)
        elif isinstance(c, EmployerRecommendation):
            results[rule.rule_code] = _employer_recommendation(rule, c, profile, as_of)
        elif isinstance(c, MinKorean):
            item = _min_korean(rule, c, scores, as_of)
            results[rule.rule_code] = item
            if item.message_code == KOREAN_DEFERRED:
                deferral_threshold = c.deferral_min_total_without_korean

    for rule in rule_set.rules_in(REQUIREMENT):
        if isinstance(rule.condition, TotalScore):
            results[rule.rule_code] = _total_score(rule, rule_set, scores, deferral_threshold)

    return [results[r.rule_code] for r in rule_set.rules_in(REQUIREMENT) if r.rule_code in results]


def calculate_exclusion_items(profile: E74Profile, rule_set: RuleSet) -> list[ItemResult]:
    """제외 항목. 해당하면 MET(신청 불가)."""
    items: list[ItemResult] = []
    for rule in rule_set.rules_in(EXCLUSION):
        c = rule.condition
        if isinstance(c, ManualReview):
            items.append(_item(rule, ItemStatus.NOT_APPLICABLE, "E74_EXCLUSION_MANUAL"))
            continue
        if not isinstance(c, DeclaredFlag | DeclaredCountAtLeast):
            continue
        value = declared_value(profile, c.declaration_field)
        evidence: dict[str, Any] = {"declarationField": c.declaration_field, "declaredValue": value}
        if value is None:
            items.append(
                _item(
                    rule,
                    ItemStatus.NEEDS_EVIDENCE,
                    "E74_EXCLUSION_NOT_DECLARED",
                    evidence,
                    missing=[declaration_missing(c.declaration_field)],
                )
            )
            continue
        applies = value is True if isinstance(c, DeclaredFlag) else int(value) >= c.min_count
        if applies:
            items.append(_item(rule, ItemStatus.MET, "E74_EXCLUSION_APPLIES", evidence))
        else:
            items.append(_item(rule, ItemStatus.NOT_MET, "E74_EXCLUSION_NONE", evidence))
    return items

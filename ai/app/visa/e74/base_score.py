"""기본항목 점수: 평균소득·한국어·나이 (명세 3.2, 정의서 2장).

공통 원칙
- 값이 없으면 0점으로 확정하지 않고 NEEDS_EVIDENCE + missingInputs로 돌려준다.
- 확정 점수(earnedPoints)는 증빙 확인된 값만, 가능 점수(potentialPoints)는 신고값까지 인정한다.
- 맞은 규칙에 reviewNote가 있거나 해석이 필요하면 NEEDS_REVIEW이고 확정 점수는 0이다.
- 증빙 상태가 REJECTED인 행은 없는 것으로 본다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.visa.e74.dates import add_months, age_on
from app.visa.e74.results import ItemResult, ItemStatus, MissingInput
from app.visa.e74.rules import (
    AgeRange,
    IncomeRange,
    KoreanLevel,
    MinAverageIncome,
    Rule,
    RuleCategory,
    RuleSet,
    RuleSetError,
)
from app.visa.e74.schemas import (
    E74Profile,
    EvidenceType,
    IncomeRecordIn,
    IncomeSource,
    ProfileEvidenceStatus,
    VerificationStatus,
    VisaEvidenceIn,
)

KOREAN_EVIDENCE_TYPES = (EvidenceType.TOPIK, EvidenceType.KIIP, EvidenceType.KIIP_PRE_TEST)
# 연도별로 한 행만 쓴다. 공식 증명원 > 사용자 신고 > 거래 합계 추정.
SOURCE_PRIORITY = {
    IncomeSource.DOCUMENT: 0,
    IncomeSource.USER_DECLARED: 1,
    IncomeSource.TRANSACTION_ESTIMATE: 2,
}
TOPIK_MAX_LEVEL = 6
KIIP_MAX_STAGE = 5
PRE_TEST_MAX_SCORE = 100


def scored_result(
    category: RuleCategory,
    best: Rule,
    verified: Rule | None,
    *,
    message_code: str,
    evidence: dict[str, Any],
    review_note: str | None = None,
) -> ItemResult:
    """점수 항목 결과. best는 신고 포함 최고 규칙, verified는 증빙 확인된 최고 규칙."""
    potential = best.points
    earned = verified.points if verified is not None else 0
    note = best.review_note or review_note
    if note is not None:
        status, earned = ItemStatus.NEEDS_REVIEW, 0
    elif verified is not None and earned == potential:
        status = ItemStatus.MET
    else:
        status = ItemStatus.NEEDS_EVIDENCE
    if verified is not None and verified is not best:
        evidence = {**evidence, "verifiedRuleCode": verified.rule_code}
    return ItemResult(
        rule_code=best.rule_code,
        category=category,
        status=status,
        earned_points=earned,
        potential_points=potential,
        message_code=message_code,
        evidence=evidence,
        review_note=note,
    )


def missing_result(
    category: RuleCategory,
    message_code: str,
    missing: list[MissingInput],
    evidence: dict[str, Any] | None = None,
) -> ItemResult:
    return ItemResult(
        rule_code=None,
        category=category,
        status=ItemStatus.NEEDS_EVIDENCE,
        earned_points=None,
        potential_points=None,
        message_code=message_code,
        evidence=evidence or {},
        missing_inputs=tuple(missing),
    )


def not_met_result(
    category: RuleCategory, message_code: str, evidence: dict[str, Any]
) -> ItemResult:
    return ItemResult(
        rule_code=None,
        category=category,
        status=ItemStatus.NOT_MET,
        earned_points=0,
        potential_points=0,
        message_code=message_code,
        evidence=evidence,
    )


# --- 나이 --------------------------------------------------------------------


def calculate_age_score(profile: E74Profile, rule_set: RuleSet, as_of: date) -> ItemResult:
    category = RuleCategory.BASE_AGE
    birth = profile.visa_profile.birth_date
    if birth is None:
        return missing_result(
            category,
            "E74_AGE_MISSING",
            [MissingInput(field="visaProfile.birthDate", question="생년월일")],
        )
    age = age_on(birth, as_of)
    evidence: dict[str, Any] = {"age": age, "asOfDate": as_of.isoformat()}
    rule = next(
        (
            r
            for r in rule_set.rules_in(category)
            if isinstance(r.condition, AgeRange)
            and r.condition.min_age <= age
            and (r.condition.max_age is None or age <= r.condition.max_age)
        ),
        None,
    )
    if rule is None:
        return ItemResult(
            rule_code=None,
            category=category,
            status=ItemStatus.NEEDS_REVIEW,
            earned_points=None,
            potential_points=None,
            message_code="E74_AGE_OUT_OF_TABLE",
            evidence=evidence,
            review_note="점수표에 없는 나이(19세 미만)",
        )
    verified = profile.visa_profile.evidence_status is ProfileEvidenceStatus.VERIFIED
    return scored_result(
        category,
        rule,
        rule if verified else None,
        message_code="E74_AGE_BAND",
        evidence=evidence,
    )


# --- 평균소득 -------------------------------------------------------------------


@dataclass(frozen=True)
class IncomeBasis:
    """소득 산정 방법(일반 MIN_AVERAGE_INCOME 행)."""

    years: int
    latest_year_required_from_month: int
    fallback_year_from: int
    fallback_year_to: int


def income_basis(rule_set: RuleSet) -> IncomeBasis:
    for rule in rule_set.rules_in(RuleCategory.REQUIREMENT):
        c = rule.condition
        if isinstance(c, MinAverageIncome) and c.industry_codes is None:
            if (
                c.income_years is None
                or c.latest_year_required_from_month is None
                or c.fallback_year_from is None
                or c.fallback_year_to is None
            ):
                break
            return IncomeBasis(
                years=c.income_years,
                latest_year_required_from_month=c.latest_year_required_from_month,
                fallback_year_from=c.fallback_year_from,
                fallback_year_to=c.fallback_year_to,
            )
    raise RuleSetError(f"{rule_set.rule_version}: no general MIN_AVERAGE_INCOME rule")


def target_income_years(as_of: date, basis: IncomeBasis) -> tuple[int, ...]:
    """평균에 쓸 연도. 전년도 증명원은 기준 월부터 발급되므로 그 전에는 한 해 앞을 쓴다."""
    latest = (
        as_of.year - 1 if as_of.month >= basis.latest_year_required_from_month else as_of.year - 2
    )
    return tuple(range(latest - basis.years + 1, latest + 1))


def _records_by_year(profile: E74Profile) -> dict[int, IncomeRecordIn]:
    best: dict[int, IncomeRecordIn] = {}
    for record in profile.income_records:
        if record.verification_status is VerificationStatus.REJECTED:
            continue
        current = best.get(record.income_year)
        if current is None or SOURCE_PRIORITY[record.source] < SOURCE_PRIORITY[current.source]:
            best[record.income_year] = record
    return best


def _is_official(record: IncomeRecordIn) -> bool:
    return (
        record.source is IncomeSource.DOCUMENT
        and record.verification_status is VerificationStatus.VERIFIED
    )


def _income_band(rule_set: RuleSet, total: int, years: int, industry: str | None) -> Rule | None:
    """합계로 비교해 평균의 반올림 오차를 피한다(years × min ≤ 합 < years × max)."""
    for rule in rule_set.rules_in(RuleCategory.BASE_INCOME):
        c = rule.condition
        if not isinstance(c, IncomeRange):
            continue
        if c.industry_codes is not None and industry not in c.industry_codes:
            continue
        if years * c.min_amount <= total and (c.max_amount is None or total < years * c.max_amount):
            return rule
    return None


def calculate_income_score(profile: E74Profile, rule_set: RuleSet, as_of: date) -> ItemResult:
    category = RuleCategory.BASE_INCOME
    basis = income_basis(rule_set)
    by_year = _records_by_year(profile)
    targets = target_income_years(as_of, basis)

    review_note: str | None = None
    if all(year in by_year for year in targets):
        used = [by_year[year] for year in targets]
    else:
        fallback = [
            record
            for year, record in by_year.items()
            if basis.fallback_year_from <= year <= basis.fallback_year_to
        ]
        if len(fallback) < basis.years:
            missing = [
                MissingInput(
                    field=f"incomeRecords[incomeYear={year}]",
                    question=f"{year}년 연간 소득(세무서 소득금액증명원 기준 금액)",
                )
                for year in targets
                if year not in by_year
            ]
            return missing_result(
                category, "E74_INCOME_MISSING", missing, {"incomeYears": list(targets)}
            )
        used = sorted(fallback, key=lambda r: r.annual_amount, reverse=True)[: basis.years]
        review_note = (
            f"최근 {basis.years}년 소득이 없어 "
            f"{basis.fallback_year_from}~{basis.fallback_year_to}년 중 높은 해로 계산함. "
            "재입국 공백 등 대체 사유 확인 필요"
        )

    total = sum(record.annual_amount for record in used)
    current = profile.current_employment
    industry = current.industry_code if current is not None else None
    evidence: dict[str, Any] = {
        "incomeYears": sorted(record.income_year for record in used),
        "averageIncome": total // basis.years,
        "sources": [record.source.value for record in sorted(used, key=lambda r: r.income_year)],
        "fallbackUsed": review_note is not None,
        "industryCode": industry,
    }

    band = _income_band(rule_set, total, basis.years, industry)
    if band is None:
        if industry is None and any(
            _income_band(rule_set, total, basis.years, code) is not None
            for code in _special_industry_codes(rule_set)
        ):
            # 특례 업종이면 점수가 있는 구간인데 현재 근무처 업종을 모른다.
            return missing_result(
                category,
                "E74_INCOME_INDUSTRY_MISSING",
                [
                    MissingInput(
                        field="employmentHistories",
                        question="현재 근무처 업종(농·축산업, 어업·내항상선은 소득 기준이 낮음)",
                    )
                ],
                evidence,
            )
        return not_met_result(category, "E74_INCOME_BELOW_MIN", evidence)

    official = all(_is_official(record) for record in used)
    return scored_result(
        category,
        band,
        band if official else None,
        message_code="E74_INCOME_BAND",
        evidence=evidence,
        review_note=review_note,
    )


def _special_industry_codes(rule_set: RuleSet) -> set[str]:
    codes: set[str] = set()
    for rule in rule_set.rules_in(RuleCategory.BASE_INCOME):
        if isinstance(rule.condition, IncomeRange) and rule.condition.industry_codes:
            codes.update(rule.condition.industry_codes)
    return codes


# --- 한국어 ---------------------------------------------------------------------


@dataclass(frozen=True)
class _KoreanCandidate:
    evidence: VisaEvidenceIn
    rule: Rule | None
    detail: dict[str, Any]
    missing: tuple[MissingInput, ...] = ()
    unreadable: bool = False


def _level_number(text: str | None) -> int | None:
    """ "3", "3급", "TOPIK 3급", "4단계"에서 숫자를 읽는다."""
    if text is None:
        return None
    match = re.search(r"\d+", text)
    return int(match.group()) if match else None


def _korean_levels(rule_set: RuleSet) -> list[tuple[Rule, KoreanLevel]]:
    return [
        (rule, rule.condition)
        for rule in rule_set.rules_in(RuleCategory.BASE_KOREAN)
        if isinstance(rule.condition, KoreanLevel)
    ]


def _in_range(value: int, low: int, high: int | None, ceiling: int) -> bool:
    return low <= value <= (high if high is not None else ceiling)


def _korean_candidate(
    ev: VisaEvidenceIn, levels: list[tuple[Rule, KoreanLevel]], as_of: date
) -> _KoreanCandidate:
    base: dict[str, Any] = {"visaEvidenceId": ev.visa_evidence_id, "type": ev.evidence_type.value}
    if ev.evidence_type is EvidenceType.KIIP_PRE_TEST:
        return _pre_test_candidate(ev, levels, as_of, base)
    number = _level_number(ev.level_value)
    if number is None:
        return _KoreanCandidate(ev, None, {**base, "levelValue": ev.level_value}, unreadable=True)
    if ev.evidence_type is EvidenceType.TOPIK:
        rule = next(
            (
                r
                for r, c in levels
                if _in_range(number, c.topik_level_min, c.topik_level_max, TOPIK_MAX_LEVEL)
            ),
            None,
        )
    else:
        rule = next(
            (
                r
                for r, c in levels
                if _in_range(number, c.kiip_stage_min, c.kiip_stage_max, KIIP_MAX_STAGE)
            ),
            None,
        )
    return _KoreanCandidate(ev, rule, {**base, "level": number})


def _pre_test_candidate(
    ev: VisaEvidenceIn,
    levels: list[tuple[Rule, KoreanLevel]],
    as_of: date,
    base: dict[str, Any],
) -> _KoreanCandidate:
    prefix = f"visaEvidences[visaEvidenceId={ev.visa_evidence_id}]"
    missing: list[MissingInput] = []
    if ev.pre_test_score is None:
        missing.append(
            MissingInput(field=f"{prefix}.preTestScore", question="사회통합프로그램 사전평가 점수")
        )
    if ev.pre_test_assigned_stage is None:
        missing.append(
            MissingInput(
                field=f"{prefix}.preTestAssignedStage", question="사전평가 후 배정받은 단계"
            )
        )
    if ev.issued_on is None:
        missing.append(MissingInput(field=f"{prefix}.issuedOn", question="사전평가 결과일"))
    if (
        missing
        or ev.pre_test_score is None
        or ev.pre_test_assigned_stage is None
        or ev.issued_on is None
    ):
        return _KoreanCandidate(ev, None, base, missing=tuple(missing))

    score, stage = ev.pre_test_score, ev.pre_test_assigned_stage
    detail = {
        **base,
        "score": score,
        "assignedStage": stage,
        "resultDate": ev.issued_on.isoformat(),
    }
    for rule, c in levels:
        if not _in_range(score, c.pre_test_score_min, c.pre_test_score_max, PRE_TEST_MAX_SCORE):
            continue
        if stage < c.pre_test_min_assigned_stage:
            return _KoreanCandidate(ev, None, {**detail, "excluded": "ASSIGNED_STAGE_TOO_LOW"})
        if as_of >= add_months(ev.issued_on, c.pre_test_validity_months):
            return _KoreanCandidate(ev, None, {**detail, "excluded": "EXPIRED"})
        return _KoreanCandidate(ev, rule, detail)
    return _KoreanCandidate(ev, None, detail)


def calculate_korean_score(profile: E74Profile, rule_set: RuleSet, as_of: date) -> ItemResult:
    category = RuleCategory.BASE_KOREAN
    levels = _korean_levels(rule_set)
    candidates = [
        _korean_candidate(ev, levels, as_of)
        for ev in profile.visa_evidences
        if ev.evidence_type in KOREAN_EVIDENCE_TYPES
        and ev.status is not VerificationStatus.REJECTED
    ]
    if not candidates:
        return missing_result(
            category,
            "E74_KOREAN_MISSING",
            [
                MissingInput(
                    field="visaEvidences[evidenceType=TOPIK|KIIP|KIIP_PRE_TEST]",
                    question="한국어 능력(TOPIK 급수, 사회통합프로그램 이수 단계, 사전평가 점수)",
                )
            ],
        )

    recognized = [(c, c.rule) for c in candidates if c.rule is not None]
    considered = [c.detail for c in candidates]
    if not recognized:
        missing = [m for c in candidates for m in c.missing]
        if missing:
            return missing_result(
                category, "E74_KOREAN_MISSING", missing, {"candidates": considered}
            )
        if any(c.unreadable for c in candidates):
            return ItemResult(
                rule_code=None,
                category=category,
                status=ItemStatus.NEEDS_REVIEW,
                earned_points=None,
                potential_points=None,
                message_code="E74_KOREAN_UNREADABLE",
                evidence={"candidates": considered},
                review_note="한국어 증빙의 급수·단계를 읽을 수 없음",
            )
        return not_met_result(category, "E74_KOREAN_BELOW_MIN", {"candidates": considered})

    def is_verified(c: _KoreanCandidate) -> bool:
        return c.evidence.status is VerificationStatus.VERIFIED

    # 신고 포함 최고점(같은 점수면 증빙 확인된 쪽), 증빙 확인된 것 중 최고점.
    best, best_rule = max(recognized, key=lambda pair: (pair[1].points or 0, is_verified(pair[0])))
    verified = [rule for c, rule in recognized if is_verified(c)]
    best_verified = max(verified, key=lambda rule: rule.points or 0) if verified else None
    return scored_result(
        category,
        best_rule,
        best_verified,
        message_code="E74_KOREAN_LEVEL",
        evidence={**best.detail, "candidates": considered},
    )

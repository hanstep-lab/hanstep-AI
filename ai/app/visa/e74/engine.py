"""E-7-4 계산 공통 진입점. 채팅 Tool과 계산 API가 같은 함수를 쓴다(00-overview 재사용 구조).

- calculate_e74: 정해진 규칙 버전으로 계산한다. DB·네트워크·LLM 없는 순수 함수.
- assess_e74: 기준일로 규칙 버전을 고른 뒤 calculate_e74를 부른다. 맞는 버전이 없으면 NEEDS_REVIEW.
- requestKey: 결과 누적·재사용 키(D7). 같은 입력이면 같은 키, 규칙 버전이 바뀌면 다른 키.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import date
from enum import StrEnum

from pydantic import Field

from app.visa.e74.base_score import (
    calculate_age_score,
    calculate_income_score,
    calculate_korean_score,
)
from app.visa.e74.bonus_penalty import calculate_bonus_items, calculate_penalties
from app.visa.e74.improvements import (
    EMPTY_ANALYSIS,
    Improvement,
    ImprovementPlan,
    ScoreGap,
    analyze_improvements,
)
from app.visa.e74.requirements import (
    KOREAN_DEFERRED,
    ScoreItems,
    calculate_exclusion_items,
    calculate_requirement_items,
    totals,
)
from app.visa.e74.results import CamelResponse, ItemResult, ItemStatus
from app.visa.e74.rules import (
    MinAverageIncome,
    MinKorean,
    RuleCategory,
    RuleSet,
    TotalScore,
    default_rule_sets,
    select_rule_set,
)
from app.visa.e74.schemas import E74Profile

BASE_CATEGORIES = (RuleCategory.BASE_INCOME, RuleCategory.BASE_KOREAN, RuleCategory.BASE_AGE)
GATE_CATEGORIES = (RuleCategory.REQUIREMENT, RuleCategory.EXCLUSION)


class EligibilityStatus(StrEnum):
    """e74_assessments.eligibility_status. 사전진단 결과이며 허가가 아니다."""

    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    NEEDS_EVIDENCE = "NEEDS_EVIDENCE"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class ScoreSummary(CamelResponse):
    """확정 점수(증빙 확인된 값만). e74_assessments의 점수 컬럼."""

    base: int
    bonus: int
    penalty: int
    total: int
    required: int | None = Field(description="적용 기준 총점(한국어 유예 시 150)")


class PotentialScore(CamelResponse):
    """신고값까지 인정한 점수. complete가 false면 모르는 항목이 있어 더 오를 수 있다."""

    base: int
    bonus: int
    penalty: int
    total: int
    complete: bool


class MissingInputSummary(CamelResponse):
    """여러 항목의 누락 입력을 필드별로 합친 것. LLM은 이 목록으로 재질의한다."""

    field: str
    question: str
    rule_codes: tuple[str, ...]


class ReviewNote(CamelResponse):
    rule_code: str | None
    note: str


class E74Assessment(CamelResponse):
    rule_version: str | None
    as_of_date: date
    request_key: str
    status: EligibilityStatus
    eligibility_confirmed: bool = Field(description="status가 ELIGIBLE일 때만 true")
    score: ScoreSummary
    potential_score: PotentialScore
    score_requirement_met: bool | None
    korean_deferral_applied: bool
    items: tuple[ItemResult, ...]
    missing_inputs: tuple[MissingInputSummary, ...]
    needs_evidence: tuple[str, ...] = Field(description="값은 있지만 증빙 서류가 필요한 규칙 코드")
    review_notes: tuple[ReviewNote, ...]
    score_gap: ScoreGap
    improvements: tuple[Improvement, ...] = Field(description="우선순위 순 개선 방안(7단계)")
    plan_to_required: ImprovementPlan


def request_key(rule_version: str | None, as_of: date, profile: E74Profile) -> str:
    """누적·재사용 키. 목록 순서가 달라도 같은 키가 나오게 정렬한 뒤 해시한다."""
    body = profile.model_dump(mode="json", by_alias=True)
    body["employmentHistories"].sort(key=lambda r: r["employmentHistoryId"])
    body["visaEvidences"].sort(key=lambda r: r["visaEvidenceId"])
    body["incomeRecords"].sort(key=lambda r: (r["incomeYear"], r["source"]))
    payload = {"ruleVersion": rule_version, "asOfDate": as_of.isoformat(), "profile": body}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _score_items(profile: E74Profile, rule_set: RuleSet, as_of: date) -> ScoreItems:
    return ScoreItems(
        income=calculate_income_score(profile, rule_set, as_of),
        korean=calculate_korean_score(profile, rule_set, as_of),
        age=calculate_age_score(profile, rule_set, as_of),
        bonuses=tuple(calculate_bonus_items(profile, rule_set, as_of)),
        penalties=tuple(calculate_penalties(profile, rule_set)),
    )


def _status(items: Sequence[ItemResult]) -> EligibilityStatus:
    gates = [item for item in items if item.category in GATE_CATEGORIES]
    if any(
        (item.category is RuleCategory.EXCLUSION and item.status is ItemStatus.MET)
        or (item.category is RuleCategory.REQUIREMENT and item.status is ItemStatus.NOT_MET)
        for item in gates
    ):
        return EligibilityStatus.INELIGIBLE
    if any(item.status is ItemStatus.NEEDS_EVIDENCE for item in gates):
        return EligibilityStatus.NEEDS_EVIDENCE
    if any(item.status is ItemStatus.NEEDS_REVIEW for item in gates):
        return EligibilityStatus.NEEDS_REVIEW
    return EligibilityStatus.ELIGIBLE


def _missing_inputs(items: Sequence[ItemResult]) -> tuple[MissingInputSummary, ...]:
    merged: dict[str, tuple[str, list[str]]] = {}
    for item in items:
        for missing in item.missing_inputs:
            question, codes = merged.setdefault(missing.field, (missing.question, []))
            code = item.rule_code or item.category.value
            if code not in codes:
                codes.append(code)
    return tuple(
        MissingInputSummary(field=field, question=question, rule_codes=tuple(codes))
        for field, (question, codes) in merged.items()
    )


def _needs_evidence(items: Sequence[ItemResult], rule_set: RuleSet) -> tuple[str, ...]:
    """값은 있지만 증빙 서류가 필요한 규칙 코드.

    소득·한국어 최소 기준과 총점 요건은 다른 항목에서 계산되는 값이라 서류 안내 대상이 아니다.
    """
    derived = {
        r.rule_code
        for r in rule_set.rules_in(RuleCategory.REQUIREMENT)
        if isinstance(r.condition, MinAverageIncome | MinKorean | TotalScore)
    }
    return tuple(
        item.rule_code
        for item in items
        if item.rule_code is not None
        and item.rule_code not in derived
        and item.status is ItemStatus.NEEDS_EVIDENCE
        and not item.missing_inputs
    )


def _sum(items: Sequence[ItemResult], categories: tuple[RuleCategory, ...], *, earned: bool) -> int:
    return sum(
        ((item.earned_points if earned else item.potential_points) or 0)
        for item in items
        if item.category in categories
    )


def calculate_e74(profile: E74Profile, as_of: date, rule_set: RuleSet) -> E74Assessment:
    scores = _score_items(profile, rule_set, as_of)
    requirements = calculate_requirement_items(profile, rule_set, as_of, scores)
    exclusions = calculate_exclusion_items(profile, rule_set)
    items = (*scores.all(), *requirements, *exclusions)

    total_codes = {
        r.rule_code
        for r in rule_set.rules_in(RuleCategory.REQUIREMENT)
        if isinstance(r.condition, TotalScore)
    }
    total_item = next((i for i in requirements if i.rule_code in total_codes), None)
    deferral = any(i.message_code == KOREAN_DEFERRED for i in requirements)
    scored = scores.all()
    score = ScoreSummary(
        base=_sum(scored, BASE_CATEGORIES, earned=True),
        bonus=_sum(scored, (RuleCategory.BONUS,), earned=True),
        penalty=_sum(scored, (RuleCategory.PENALTY,), earned=True),
        total=totals(scored).confirmed,
        required=(
            total_item.evidence.get("requiredTotal", rule_set.required_total_score)
            if total_item is not None
            else rule_set.required_total_score
        ),
    )
    potential_base = _sum(scored, BASE_CATEGORIES, earned=False)
    potential_bonus = _sum(scored, (RuleCategory.BONUS,), earned=False)
    potential_penalty = _sum(scored, (RuleCategory.PENALTY,), earned=False)
    potential = PotentialScore(
        base=potential_base,
        bonus=potential_bonus,
        penalty=potential_penalty,
        total=potential_base + potential_bonus + potential_penalty,
        complete=totals(scored).complete,
    )
    status = _status(items)
    score_met: bool | None = None
    if total_item is not None and total_item.status is ItemStatus.MET:
        score_met = True
    elif total_item is not None and total_item.status is ItemStatus.NOT_MET:
        score_met = False
    analysis = analyze_improvements(
        profile,
        rule_set,
        as_of,
        items,
        required=score.required,
        confirmed_total=score.total,
        potential_total=potential.total,
    )

    return E74Assessment(
        rule_version=rule_set.rule_version,
        as_of_date=as_of,
        request_key=request_key(rule_set.rule_version, as_of, profile),
        status=status,
        eligibility_confirmed=status is EligibilityStatus.ELIGIBLE,
        score=score,
        potential_score=potential,
        score_requirement_met=score_met,
        korean_deferral_applied=deferral,
        items=items,
        missing_inputs=_missing_inputs(items),
        needs_evidence=_needs_evidence(items, rule_set),
        review_notes=tuple(
            ReviewNote(rule_code=item.rule_code, note=item.review_note)
            for item in items
            if item.review_note is not None
        ),
        score_gap=analysis.score_gap,
        improvements=analysis.improvements,
        plan_to_required=analysis.plan,
    )


def assess_e74(
    profile: E74Profile,
    as_of: date,
    *,
    rule_sets: Sequence[RuleSet] | None = None,
    include_draft: bool = False,
) -> E74Assessment:
    """기준일에 맞는 규칙 버전으로 계산한다. 채팅 Tool과 계산 API의 공통 진입점."""
    available = default_rule_sets() if rule_sets is None else rule_sets
    rule_set = select_rule_set(as_of, available, include_draft=include_draft)
    if rule_set is not None:
        return calculate_e74(profile, as_of, rule_set)
    empty = ScoreSummary(base=0, bonus=0, penalty=0, total=0, required=None)
    return E74Assessment(
        rule_version=None,
        as_of_date=as_of,
        request_key=request_key(None, as_of, profile),
        status=EligibilityStatus.NEEDS_REVIEW,
        eligibility_confirmed=False,
        score=empty,
        potential_score=PotentialScore(base=0, bonus=0, penalty=0, total=0, complete=False),
        score_requirement_met=None,
        korean_deferral_applied=False,
        items=(),
        missing_inputs=(),
        needs_evidence=(),
        review_notes=(
            ReviewNote(
                rule_code=None,
                note=f"{as_of.isoformat()}에 적용할 승인된 규칙 버전이 없음(Q9)",
            ),
        ),
        score_gap=EMPTY_ANALYSIS.score_gap,
        improvements=EMPTY_ANALYSIS.improvements,
        plan_to_required=EMPTY_ANALYSIS.plan,
    )

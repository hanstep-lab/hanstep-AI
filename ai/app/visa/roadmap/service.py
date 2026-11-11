"""E-7-4 로드맵 만들기: 엔진 계산 → 신청 가능일 → 단계 고르기 → 예상 점수 → 경고 → 문장.

HTTP·DB와 무관하다. 점수·날짜·금액은 모두 엔진과 이 코드가 계산하고 LLM은 문장만 쓴다.
저장은 하지 않는다(Spring이 visa_roadmaps에 저장).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any, Protocol

from pydantic import ValidationError

from app.visa.e74.dates import full_months_between
from app.visa.e74.engine import E74Assessment, assess_e74
from app.visa.e74.improvements import Improvement, ImprovementKind
from app.visa.e74.results import ItemResult, ItemStatus
from app.visa.e74.rules import (
    ApprovalStatus,
    RuleActor,
    RuleCategory,
    RuleSet,
    Tenure,
    default_rule_sets,
)
from app.visa.e74.scenario import apply_scenario
from app.visa.e74.schemas import E74Profile, ProfileEvidenceStatus
from app.visa.roadmap.schemas import (
    Basis,
    ProjectedScore,
    RoadmapRequest,
    RoadmapResponse,
    RoadmapScore,
    RoadmapStep,
    RoadmapWarning,
    ScoreItemView,
    StayInfo,
    StepKind,
    TextInfo,
    TextSource,
)
from app.visa.roadmap.steps import applicable_from, scenario_for, select_steps
from app.visa.roadmap.text import (
    LlmTextError,
    StepText,
    template_apply_text,
    template_step_text,
)

BASE_CATEGORIES = (RuleCategory.BASE_INCOME, RuleCategory.BASE_KOREAN, RuleCategory.BASE_AGE)


class StepWriter(Protocol):
    """LLM 문장 작성기(text.LlmStepWriter). 테스트에서 가짜로 바꾼다."""

    model_id: str
    prompt_version: str

    def write(self, steps: Sequence[dict[str, Any]]) -> dict[int, StepText]: ...


# --- 점수 항목 ---------------------------------------------------------------------------


def _basis(item: ItemResult, profile: E74Profile) -> Basis | None:
    ev = item.evidence
    if item.category is RuleCategory.BASE_INCOME:
        sources = ev.get("sources") or []
        if Basis.TRANSACTION_ESTIMATE.value in sources:
            return Basis.TRANSACTION_ESTIMATE
        if sources and all(s == "DOCUMENT" for s in sources):
            return Basis.DOCUMENT
        return None
    if item.category is RuleCategory.BASE_KOREAN:
        kind = ev.get("type")
        return Basis(kind) if isinstance(kind, str) and kind in Basis.__members__ else None
    if item.category is RuleCategory.BASE_AGE:
        verified = profile.visa_profile.evidence_status is ProfileEvidenceStatus.VERIFIED
        return Basis.ARC if verified and "age" in ev else None
    return None


def _value(item: ItemResult, rule_set: RuleSet, as_of: date) -> dict[str, Any]:
    ev = item.evidence
    if item.category is RuleCategory.BASE_INCOME and "averageIncome" in ev:
        return {"averageIncome": ev["averageIncome"]}
    if item.category is RuleCategory.BASE_KOREAN and "level" in ev:
        return {"level": ev["level"]}
    if item.category is RuleCategory.BASE_AGE and "age" in ev:
        return {"age": ev["age"]}
    rule = rule_set.rule(item.rule_code) if item.rule_code else None
    covered = ev.get("coveredDays")
    if rule is not None and isinstance(rule.condition, Tenure) and isinstance(covered, int):
        return {"tenureMonths": full_months_between(as_of - timedelta(days=covered), as_of)}
    return {}


def _score_items(
    assessment: E74Assessment,
    rule_set: RuleSet,
    profile: E74Profile,
    steps: Sequence[Improvement],
) -> tuple[ScoreItemView, ...]:
    """기본점수 3개 + 받았거나 단계에 나온 가점. 요건·제외·감점 항목은 넣지 않는다."""
    gains = {imp.rule_code: imp.points_gain for imp in steps}
    blocking = {imp.rule_code for imp in steps if imp.blocking}
    views: list[ScoreItemView] = []
    for item in assessment.items:
        if item.category in BASE_CATEGORIES:
            potential = item.potential_points
        elif item.category is RuleCategory.BONUS:
            earned = item.earned_points or 0
            gain = gains.get(item.rule_code or "")
            if earned <= 0 and gain is None:
                continue
            potential = item.potential_points
            if item.status is ItemStatus.NOT_MET and gain is not None:
                potential = earned + gain
        else:
            continue
        views.append(
            ScoreItemView(
                rule_code=item.rule_code,
                category=item.category,
                status=item.status,
                earned_points=item.earned_points,
                potential_points=potential,
                basis=_basis(item, profile),
                value=_value(item, rule_set, assessment.as_of_date),
                required_for_application=item.rule_code in blocking,
            )
        )
    # 기본점수 → 받은 가점 → 단계로 얻을 가점 순(화면 순서).
    return tuple(
        sorted(
            views,
            key=lambda v: (
                v.category is RuleCategory.BONUS,
                v.category is RuleCategory.BONUS and (v.earned_points or 0) <= 0,
            ),
        )
    )


# --- 예상 점수 ---------------------------------------------------------------------------


def _projected_total(
    steps: Sequence[Improvement],
    assessment: E74Assessment,
    rule_set: RuleSet,
    profile: E74Profile,
    rule_sets: Sequence[RuleSet],
    include_draft: bool,
) -> int | None:
    if not steps:
        return assessment.score.total
    scenario = scenario_for(steps, assessment, rule_set, profile)
    if scenario is None:
        return None
    as_of = assessment.as_of_date
    try:
        assumed = apply_scenario(profile, scenario, as_of)
    except ValidationError:
        return None
    rerun = assess_e74(assumed, as_of, rule_sets=rule_sets, include_draft=include_draft)
    if rerun.rule_version != assessment.rule_version:
        return None
    return rerun.score.total


# --- 문장 --------------------------------------------------------------------------------


def _llm_input(steps: Sequence[RoadmapStep]) -> list[dict[str, Any]]:
    """LLM에 넘기는 단계 데이터. 프로필 원본은 넘기지 않는다."""
    return [
        {
            "order": s.order,
            "kind": s.kind.value,
            "actor": s.actor.value,
            "required": s.required,
            "estimatedAdditionalScore": s.estimated_additional_score,
            "availableFrom": s.available_from.isoformat() if s.available_from else None,
            "targetDate": s.target_date.isoformat() if s.target_date else None,
            "requiredEvidence": list(s.required_evidence),
            "templateTitle": s.title,
            "templateDescription": s.description,
        }
        for s in steps
    ]


def _write_text(
    steps: tuple[RoadmapStep, ...], writer: StepWriter | None
) -> tuple[tuple[RoadmapStep, ...], TextInfo]:
    template = TextInfo(source=TextSource.TEMPLATE, model_id=None, prompt_version=None)
    if writer is None:
        return steps, template
    try:
        texts = writer.write(_llm_input(steps))
    except LlmTextError as exc:
        return steps, template.model_copy(update={"fallback_reason": exc.reason})
    rewritten = tuple(
        s.model_copy(
            update={"title": texts[s.order].title, "description": texts[s.order].description}
        )
        for s in steps
    )
    return rewritten, TextInfo(
        source=TextSource.LLM, model_id=writer.model_id, prompt_version=writer.prompt_version
    )


# --- 진입점 ------------------------------------------------------------------------------


def _warnings(
    rule_set: RuleSet, applicable: date | None, expires: date
) -> tuple[RoadmapWarning, ...]:
    warnings: list[RoadmapWarning] = []
    if rule_set.approval_status is ApprovalStatus.DRAFT:
        warnings.append(RoadmapWarning.DRAFT_RULES)
    end = rule_set.effective_to
    if end is not None and any(d is not None and d >= end for d in (applicable, expires)):
        warnings.append(RoadmapWarning.RULES_EXPIRE)
    if applicable is not None and applicable > expires:
        warnings.append(RoadmapWarning.STAY_EXPIRES_FIRST)
    return tuple(warnings)


def _action_step(order: int, imp: Improvement, rule_set: RuleSet) -> RoadmapStep:
    text = template_step_text(imp, rule_set.rule(imp.rule_code), required=imp.blocking)
    return RoadmapStep(
        order=order,
        kind=StepKind(imp.kind.value),
        rule_code=imp.rule_code,
        actor=imp.actor,
        required=imp.blocking,
        estimated_additional_score=imp.points_gain,
        available_from=imp.available_from,
        target_date=None,
        required_evidence=imp.documents,
        title=text.title,
        description=text.description,
    )


def _rule_set_of(assessment: E74Assessment, rule_sets: Sequence[RuleSet]) -> RuleSet | None:
    return next((rs for rs in rule_sets if rs.rule_version == assessment.rule_version), None)


def build_roadmap(
    request: RoadmapRequest,
    *,
    include_draft: bool,
    rule_sets: Sequence[RuleSet] | None = None,
    writer: StepWriter | None = None,
) -> RoadmapResponse:
    available = default_rule_sets() if rule_sets is None else rule_sets
    profile, as_of, expires = request.profile, request.as_of_date, request.stay_expires_on
    assessment = assess_e74(profile, as_of, rule_sets=available, include_draft=include_draft)
    rule_set = _rule_set_of(assessment, available)

    applicable: date | None = None
    chosen: list[Improvement] = []
    steps: list[RoadmapStep] = []
    if rule_set is not None:
        applicable = applicable_from(assessment, rule_set)
        chosen = select_steps(assessment, rule_set)
        steps = [_action_step(order, imp, rule_set) for order, imp in enumerate(chosen, start=1)]
    apply_text = template_apply_text(applicable, expires)
    steps.append(
        RoadmapStep(
            order=len(steps) + 1,
            kind=StepKind.APPLY,
            rule_code=None,
            actor=RuleActor.USER,
            required=True,
            estimated_additional_score=None,
            available_from=applicable,
            target_date=expires,
            required_evidence=(),
            title=apply_text.title,
            description=apply_text.description,
        )
    )
    final_steps, text_info = _write_text(tuple(steps), writer)

    if rule_set is None:
        projected = ProjectedScore(after_required_steps=None, after_all_steps=None)
        warnings: tuple[RoadmapWarning, ...] = ()
        rule_status = None
    else:
        required_steps = [
            imp for imp in chosen if imp.blocking or imp.kind is ImprovementKind.SUBMIT_EVIDENCE
        ]
        projected = ProjectedScore(
            after_required_steps=_projected_total(
                required_steps, assessment, rule_set, profile, available, include_draft
            ),
            after_all_steps=_projected_total(
                chosen, assessment, rule_set, profile, available, include_draft
            ),
        )
        warnings = _warnings(rule_set, applicable, expires)
        rule_status = rule_set.approval_status

    visa = profile.visa_profile
    return RoadmapResponse(
        as_of_date=as_of,
        rule_version=assessment.rule_version,
        rule_status=rule_status,
        assessment_request_key=assessment.request_key,
        stay=StayInfo(
            visa_type_code=visa.visa_type_code,
            stay_started_on=visa.current_stay_started_on,
            stay_expires_on=expires,
            days_to_expiry=(expires - as_of).days,
            applicable_from=applicable,
        ),
        score=RoadmapScore(
            confirmed=assessment.score.total,
            potential=assessment.potential_score.total,
            items=_score_items(assessment, rule_set, profile, chosen)
            if rule_set is not None
            else (),
        ),
        steps=final_steps,
        projected_score=projected,
        missing_inputs=assessment.missing_inputs,
        warnings=warnings,
        text=text_info,
    )

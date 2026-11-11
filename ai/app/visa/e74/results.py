"""E-7-4 항목별 판정 결과. MySQL e74_assessment_items와 같은 모양에 응답 전용 필드를 더한다.

- earnedPoints: 증빙 확인된 값으로만 계산한 확정 점수(e74_assessment_items.earned_points)
- potentialPoints: 신고값까지 인정하면 받을 점수(증빙을 내면 받을 수 있는 점수)
- missingInputs: 판정에 필요한데 없는 입력. LLM이 사용자에게 다시 묻는 데 쓴다.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.visa.e74.rules import Rule, RuleCategory


class CamelResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)


class ItemStatus(StrEnum):
    """e74_assessment_items.status."""

    MET = "MET"
    NOT_MET = "NOT_MET"
    NEEDS_EVIDENCE = "NEEDS_EVIDENCE"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class MissingInput(CamelResponse):
    """판정에 필요한 입력. field는 요청 JSON 경로, question은 LLM이 사용자에게 물을 내용."""

    field: str
    question: str


class ItemResult(CamelResponse):
    rule_code: str | None = Field(description="적용된 규칙. 판정하지 못했으면 null")
    category: RuleCategory
    status: ItemStatus
    earned_points: int | None = Field(description="확정 점수(증빙 확인된 값만)")
    potential_points: int | None = Field(description="신고값까지 인정할 때의 점수")
    message_code: str
    evidence: dict[str, Any] = Field(default_factory=dict, description="판정에 쓴 값과 증빙 ID")
    missing_inputs: tuple[MissingInput, ...] = ()
    review_note: str | None = None


# --- 결과 생성 도우미 -------------------------------------------------------------


def scored_result(
    category: RuleCategory,
    best: Rule,
    verified: Rule | None,
    *,
    message_code: str,
    evidence: dict[str, Any],
    review_note: str | None = None,
    missing: tuple[MissingInput, ...] = (),
) -> ItemResult:
    """점수 항목 결과. best는 신고 포함 최고 규칙, verified는 증빙 확인된 최고 규칙.

    reviewNote가 있으면 NEEDS_REVIEW(확정 0), 확정 점수가 가능 점수와 같으면 MET,
    아니면 NEEDS_EVIDENCE(증빙을 더 내면 점수가 오른다).
    """
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
        missing_inputs=missing,
        review_note=note,
    )


def missing_result(
    category: RuleCategory,
    message_code: str,
    missing: list[MissingInput],
    evidence: dict[str, Any] | None = None,
    *,
    rule_code: str | None = None,
) -> ItemResult:
    """판정에 필요한 값이 없다: 점수를 확정하지 않고 재질의 목록을 돌려준다."""
    return ItemResult(
        rule_code=rule_code,
        category=category,
        status=ItemStatus.NEEDS_EVIDENCE,
        earned_points=None,
        potential_points=None,
        message_code=message_code,
        evidence=evidence or {},
        missing_inputs=tuple(missing),
    )


def not_met_result(
    category: RuleCategory,
    message_code: str,
    evidence: dict[str, Any],
    *,
    rule_code: str | None = None,
) -> ItemResult:
    return ItemResult(
        rule_code=rule_code,
        category=category,
        status=ItemStatus.NOT_MET,
        earned_points=0,
        potential_points=0,
        message_code=message_code,
        evidence=evidence,
    )

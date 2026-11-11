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

from app.visa.e74.rules import RuleCategory


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

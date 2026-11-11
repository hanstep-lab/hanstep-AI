"""로드맵 API 요청·응답 모양. Spring이 응답을 visa_roadmaps, visa_roadmap_steps에 저장한다."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from app.visa.e74.engine import MissingInputSummary
from app.visa.e74.improvements import ImprovementKind
from app.visa.e74.results import CamelResponse, ItemStatus
from app.visa.e74.rules import ApprovalStatus, RuleActor, RuleCategory
from app.visa.e74.schemas import CamelRequest, E74Profile


class RoadmapRequest(CamelRequest):
    """POST /internal/v1/visa/roadmap 요청. 본인 확인은 Spring이 한다."""

    as_of_date: date = Field(description="계산 기준일. Spring이 정해서 보낸다")
    stay_expires_on: date = Field(
        description="현재 체류 만료일(외국인등록증). 프로필에 없어 따로 받는다"
    )
    profile: E74Profile

    @model_validator(mode="after")
    def _not_expired(self) -> Self:
        if self.stay_expires_on < self.as_of_date:
            raise ValueError("stayExpiresOn must not be before asOfDate")
        return self


class StepKind(StrEnum):
    """단계 종류: 엔진 개선 방안 종류 + 마지막 신청 단계.

    기다리기(WAIT_UNTIL)는 단계로 만들지 않고 신청 가능일에 반영한다.
    """

    FIX_REQUIREMENT = ImprovementKind.FIX_REQUIREMENT.value
    SUBMIT_EVIDENCE = ImprovementKind.SUBMIT_EVIDENCE.value
    RAISE_LEVEL = ImprovementKind.RAISE_LEVEL.value
    OBTAIN_BONUS = ImprovementKind.OBTAIN_BONUS.value
    APPLY = "APPLY"


class Basis(StrEnum):
    """점수 근거 라벨 키. 화면이 한국어 라벨로 바꾼다."""

    TRANSACTION_ESTIMATE = "TRANSACTION_ESTIMATE"  # 마이데이터 추정
    DOCUMENT = "DOCUMENT"  # 증빙 확인
    TOPIK = "TOPIK"  # 성적 증빙
    KIIP = "KIIP"
    KIIP_PRE_TEST = "KIIP_PRE_TEST"
    ARC = "ARC"  # 외국인등록증


class RoadmapWarning(StrEnum):
    DRAFT_RULES = "DRAFT_RULES"  # 적용 규칙이 승인 전(DRAFT)
    RULES_EXPIRE = "RULES_EXPIRE"  # 신청 가능일이나 체류 만료일이 규칙 유효기간 밖
    STAY_EXPIRES_FIRST = "STAY_EXPIRES_FIRST"  # 체류 만료일이 신청 가능일보다 먼저


class TextSource(StrEnum):
    TEMPLATE = "TEMPLATE"
    LLM = "LLM"


class StayInfo(CamelResponse):
    visa_type_code: str | None
    stay_started_on: date | None
    stay_expires_on: date
    days_to_expiry: int
    applicable_from: date | None = Field(
        description="신청 가능일. 체류기간을 판정할 수 없으면(재질의 필요) null"
    )


class ScoreItemView(CamelResponse):
    """화면에 보이는 점수 항목: 기본점수 3개 + 받았거나 단계에 나온 가점."""

    rule_code: str | None
    category: RuleCategory
    status: ItemStatus
    earned_points: int | None
    potential_points: int | None = Field(
        description="신고값까지 인정한 점수. 단계로 얻을 가점이면 그 단계 점수"
    )
    basis: Basis | None = None
    value: dict[str, Any] = Field(default_factory=dict)
    required_for_application: bool = Field(
        default=False, description="필수요건이기도 해서 없으면 신청할 수 없는 항목"
    )


class RoadmapScore(CamelResponse):
    confirmed: int = Field(description="증빙 확인된 값만으로 계산한 총점")
    potential: int = Field(description="신고값까지 인정한 총점")
    # 법무부 공고 확인 전에는 기준 점수를 내보내지 않는다. 엔진 내부 계산에는 쓴다.
    required: None = None
    items: tuple[ScoreItemView, ...]


class RoadmapStep(CamelResponse):
    order: int
    kind: StepKind
    rule_code: str | None
    actor: RuleActor
    required: bool = Field(description="신청하려면 꼭 해야 하는 단계")
    estimated_additional_score: int | None = Field(
        description="이 단계만 했을 때 오르는 확정 점수(엔진 계산). 요건·신청 단계는 null"
    )
    available_from: date | None
    target_date: date | None
    required_evidence: tuple[str, ...]
    title: str
    description: str


class ProjectedScore(CamelResponse):
    """단계를 적용한 프로필로 엔진을 다시 돌린 확정 총점. 계산할 수 없으면 null."""

    after_required_steps: int | None = Field(description="필수 단계 + 증빙 제출 단계 적용")
    after_all_steps: int | None


class TextInfo(CamelResponse):
    source: TextSource
    model_id: str | None
    prompt_version: str | None
    fallback_reason: str | None = Field(
        default=None, description="LLM 문장을 쓰지 않고 템플릿으로 되돌린 이유"
    )


class RoadmapResponse(CamelResponse):
    goal_visa_code: Literal["E-7-4"] = "E-7-4"
    as_of_date: date
    rule_version: str | None
    rule_status: ApprovalStatus | None
    assessment_request_key: str
    stay: StayInfo
    score: RoadmapScore
    steps: tuple[RoadmapStep, ...]
    projected_score: ProjectedScore
    missing_inputs: tuple[MissingInputSummary, ...] = Field(
        description="판정에 필요한데 없는 입력. 다시 물어야 할 항목"
    )
    warnings: tuple[RoadmapWarning, ...]
    text: TextInfo
    is_mock: bool = Field(default=True, description="모의 사전진단 결과(실제 심사 결과가 아님)")

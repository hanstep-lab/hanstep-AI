"""E-7-4 계산 API 업무 흐름(10단계): 시나리오 적용 → 공통 진입점 assess_e74 → 응답 조립.

HTTP와 무관하다. 시나리오를 반영한 프로필이 형식에 맞지 않으면 E74ScenarioError를 올리고
API 계층이 400으로 바꾼다. 저장은 하지 않는다(누적·재사용은 Spring, D7).
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import Field, ValidationError

from app.visa.e74.engine import E74Assessment, assess_e74, request_key
from app.visa.e74.rules import ApprovalStatus, RuleSet, default_rule_sets
from app.visa.e74.scenario import apply_scenario, scenario_as_of
from app.visa.e74.schemas import E74CalculateRequest, E74Scenario


class E74ScenarioError(ValueError):
    """시나리오 적용 결과가 프로필 형식에 맞지 않음. 필드 경로와 사유만 담는다(값은 담지 않음)."""

    def __init__(self, fields: list[dict[str, str]]) -> None:
        super().__init__("scenario produces an invalid profile")
        self.fields = fields


class E74CalculateResponse(E74Assessment):
    """엔진 결과 + 저장·표시에 필요한 정보."""

    rule_status: ApprovalStatus | None = Field(
        description="적용한 규칙 버전 상태. DRAFT면 승인 전 점수표(화면에 표시)"
    )
    is_scenario: bool = Field(
        description="가정 시나리오 계산이면 true(e74_assessments.is_scenario)"
    )
    scenario: E74Scenario | None = Field(description="받은 가정 조건 그대로(scenario_json)")


def calculate(
    request: E74CalculateRequest,
    *,
    include_draft: bool,
    rule_sets: Sequence[RuleSet] | None = None,
) -> E74CalculateResponse:
    available = default_rule_sets() if rule_sets is None else rule_sets
    scenario = request.scenario
    as_of = scenario_as_of(request.as_of_date, scenario)
    profile = request.profile
    if scenario is not None:
        try:
            profile = apply_scenario(profile, scenario, as_of)
        except ValidationError as exc:
            fields = [
                {"field": "profile." + ".".join(str(p) for p in err["loc"]), "reason": err["type"]}
                for err in exc.errors()
            ]
            raise E74ScenarioError(fields) from exc

    assessment = assess_e74(profile, as_of, rule_sets=available, include_draft=include_draft)
    key = assessment.request_key
    if scenario is not None:
        # 원래 프로필 + 요청 기준일 + 시나리오로 키를 만든다(기본 계산과 겹치지 않게).
        key = request_key(
            assessment.rule_version, request.as_of_date, request.profile, scenario=scenario
        )
    rule_status = next(
        (rs.approval_status for rs in available if rs.rule_version == assessment.rule_version),
        None,
    )
    return E74CalculateResponse(
        **{**dict(assessment), "request_key": key},
        rule_status=rule_status,
        is_scenario=scenario is not None,
        scenario=scenario,
    )

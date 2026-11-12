"""채팅 에이전트가 쓰는 비자 도구의 입력·출력 형식. 도구 실행과 채팅 API 구현은 채팅 담당이 한다.

도구 3개: 정책 검색(search_visa_policies), E-7-4 점수(calculate_e74_score),
로드맵(get_visa_roadmap).

- 도구 입력은 LLM이 채우는 인자다. camelCase이고 모르는 필드는 거부한다. 사용자 식별값은 인자에
  없다. 도구를 실행하는 쪽이 로그인 사용자 id를 넣어 Spring을 부른다(남의 데이터 조회 차단).
- 결과를 저장해야 하는 도구(E-7-4, 로드맵)는 Spring 내부 API를 거친다. Spring이 저장한 행 id가 채팅
  기록의 결과 참조(chat_tool_calls.result_ref_*)와 근거(chat_message_sources)가 된다. 세션이 끝난 뒤
  다시 볼 때는 그 행을 읽는다(재계산·LLM 재호출 없음).
- 화면 카드(chat_messages.blocks_json)의 숫자는 도구 결과에서만 채운다. LLM이 쓴 숫자를 넣지 않는다.
- ``*_records`` 함수가 도구 결과 하나로 LLM에 돌려줄 요약, 결과 참조, 근거, 카드를 한 번에 만든다.
- 채팅 기록(chat_tool_calls.arguments_json)에는 LLM 인자를 그대로 남기지 않고
  ``recorded_arguments``를 거친 값을 남긴다(감점·제외 6문항 답 값 제외).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.rag.retrieval import SearchHit
from app.visa.e74.engine import EligibilityStatus
from app.visa.e74.results import ItemStatus
from app.visa.e74.rules import ApprovalStatus, RuleCategory
from app.visa.e74.schemas import E74Scenario, LegalDeclarationIn
from app.visa.e74.service import E74CalculateResponse
from app.visa.roadmap.schemas import RoadmapResponse

SEARCH_VISA_POLICIES = "search_visa_policies"
CALCULATE_E74_SCORE = "calculate_e74_score"
GET_VISA_ROADMAP = "get_visa_roadmap"

# LLM에 돌려주는 개선 방안 수와 검색 본문 길이(컨텍스트 절약).
TOP_IMPROVEMENTS = 8
SEARCH_TEXT_CHARS = 1200


class ToolInput(BaseModel):
    """도구 인자 공통 설정: camelCase만 받고 모르는 필드는 거부한다."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=False, extra="forbid")


class ChatRecord(BaseModel):
    """채팅 기록·Spring 계약 공통 설정: camelCase로 내보내고 만든 뒤에는 바꿀 수 없다."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)


# --- 1. 도구 입력(LLM이 채우는 인자) ------------------------------------------------------


class SearchVisaPoliciesInput(ToolInput):
    """승인된 비자 정책 문서 검색. 제도·요건·서류·일정 질문에 답하기 전에 쓴다."""

    query: str = Field(min_length=1, max_length=1000, description="검색할 내용(한국어 권장)")
    top_k: int = Field(default=5, ge=1, le=8, description="돌려받을 결과 수")
    visa_types: list[str] | None = Field(
        default=None, max_length=10, description="비자 종류 필터(예: E-7-4). 없으면 전체"
    )


class CalculateE74ScoreInput(ToolInput):
    """사용자 프로필로 E-7-4 점수와 신청 가능 여부를 계산한다. 점수 질문에는 반드시 쓴다."""

    as_of_date: date | None = Field(default=None, description="기준일. 없으면 오늘(KST)")
    scenario: E74Scenario | None = Field(
        default=None, description="가정 조건(증빙 취득, 소득 변경, 기준일 이동). 없으면 실제 상태"
    )
    legal_declaration: LegalDeclarationIn | None = Field(
        default=None,
        description="감점·제외 6문항에 대한 사용자 답. 저장하지 않으므로 물어서 받은 답만 넣는다",
    )


class GetVisaRoadmapInput(ToolInput):
    """E-7-4 전환 로드맵(단계·예상 점수·신청 가능일)을 만든다."""

    as_of_date: date | None = Field(default=None, description="기준일. 없으면 오늘(KST)")


TOOL_INPUTS: dict[str, type[ToolInput]] = {
    SEARCH_VISA_POLICIES: SearchVisaPoliciesInput,
    CALCULATE_E74_SCORE: CalculateE74ScoreInput,
    GET_VISA_ROADMAP: GetVisaRoadmapInput,
}


def tool_definitions() -> list[dict[str, Any]]:
    """LLM 도구 정의(name, description, input_schema). 설명은 입력 모델의 docstring이다."""
    definitions = []
    for name, model in TOOL_INPUTS.items():
        schema = model.model_json_schema(by_alias=True)
        description = schema.pop("description", "")
        schema.pop("title", None)
        definitions.append({"name": name, "description": description, "input_schema": schema})
    return definitions


# 채팅 기록에 값을 남기지 않는 인자: 감점·제외 6문항 답(벌금·체납·출입국 위반 등 자기신고).
LEGAL_DECLARATION_KEY = to_camel("legal_declaration")
LEGAL_DECLARATION_ITEMS = tuple(
    field.alias or name for name, field in LegalDeclarationIn.model_fields.items()
)


def recorded_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """채팅 기록에 남길 도구 인자(agent.tool_call의 arguments → chat_tool_calls.arguments_json).

    도구 실행에는 원래 인자를 쓰고, 기록에는 이 값을 쓴다. legalDeclaration은 값 대신 답한 문항
    이름만 남긴다(예: {"answered": ["finesUnder1mCount"]}). LLM이 인자를 잘못 채워 다른 도구에
    넣거나 모양이 틀려도 값이 남지 않게, 도구 이름이나 형식 검사와 상관없이 바꾼다.
    """
    recorded = dict(arguments)
    declaration = recorded.get(LEGAL_DECLARATION_KEY)
    if declaration is not None:
        answers = declaration if isinstance(declaration, Mapping) else {}
        recorded[LEGAL_DECLARATION_KEY] = {
            "answered": [key for key in LEGAL_DECLARATION_ITEMS if answers.get(key) is not None]
        }
    return recorded


# --- 2. 도구가 부르는 Spring 내부 API(AI → Spring, 경로는 백엔드와 확정 전 초안) ---------------
#
# POST /internal/v1/ai-context/users/{userId}/e74-assessments  본문 E74AssessmentToolRequest
#   Spring이 MySQL에서 프로필 스냅샷을 만들어 AI 계산 API를 부르고 e74_assessments에 저장한다.
#   같은 requestKey면 새로 저장하지 않고 저장된 결과를 돌려준다(reused=true).
# POST /internal/v1/ai-context/users/{userId}/visa-roadmaps    본문 RoadmapToolRequest
#   Spring이 AI 로드맵 API를 부르고 visa_roadmaps·visa_roadmap_steps에 저장한다.


class E74AssessmentToolRequest(ChatRecord):
    """E-7-4 평가 생성 요청. 사용자 id는 경로에 넣는다."""

    as_of_date: date
    scenario: E74Scenario | None = None
    legal_declaration: LegalDeclarationIn | None = None


class E74AssessmentToolResponse(ChatRecord):
    """Spring이 저장한 평가 행 id와 AI 계산 API 응답(저장된 결과면 그대로)."""

    assessment_id: int = Field(ge=1, description="e74_assessments.id")
    reused: bool = Field(description="같은 requestKey로 이미 저장된 결과를 돌려줬는지")
    result: E74CalculateResponse


class RoadmapToolRequest(ChatRecord):
    """로드맵 생성 요청. 사용자 id는 경로에 넣는다."""

    as_of_date: date


class RoadmapToolResponse(ChatRecord):
    """Spring이 저장한 로드맵 행 id와 AI 로드맵 API 응답."""

    roadmap_id: int = Field(ge=1, description="visa_roadmaps.id")
    roadmap: RoadmapResponse


# --- 3. 채팅 기록 형식(chat_tool_calls, chat_message_sources, chat_messages.blocks_json) ------


class ResultRefType(StrEnum):
    """chat_tool_calls.result_ref_type 중 비자 도구가 쓰는 값."""

    E74_ASSESSMENT = "E74_ASSESSMENT"
    VISA_ROADMAP = "VISA_ROADMAP"


class SourceType(StrEnum):
    """chat_message_sources.source_type 중 비자 도구가 쓰는 값(DDL enum과 같은 철자)."""

    POLICY_DOCUMENT = "POLICY_DOCUMENT"
    E74_ASSESSMENT = "E74_ASSESSMENT"
    VISA_ROADMAP = "VISA_ROADMAP"


class ResultRef(ChatRecord):
    """도구 결과가 저장된 행(chat_tool_calls.result_ref_type, result_ref_id)."""

    type: ResultRefType
    id: int = Field(ge=1)


class SourceRef(ChatRecord):
    """답변 근거 한 건(chat_message_sources 한 행).

    키는 agent.sources와 Spring CHT-04가 읽는 {type, id, chunkRef, pageLabel}이다.
    Spring은 type·id가 없는 근거를 오류 없이 버린다.
    """

    type: SourceType
    id: int = Field(ge=1, description="정책 문서 id, e74_assessments.id, visa_roadmaps.id")
    chunk_ref: str | None = Field(default=None, max_length=100, description="MongoDB 청크 id")
    page_label: str | None = Field(default=None, max_length=50, description="쪽·절")


class E74ScoreCardBlock(ChatRecord):
    """E-7-4 점수 카드. 자세한 값은 assessmentId로 e74_assessments.result_json에서 읽는다."""

    type: Literal["E74_SCORE_CARD"] = "E74_SCORE_CARD"
    assessment_id: int = Field(ge=1)
    status: EligibilityStatus
    total: int = Field(description="확정 총점(증빙 확인된 값만)")
    potential_total: int = Field(description="신고값까지 인정한 총점")
    required: int | None = Field(description="적용 기준 총점(한국어 유예 시 150)")
    rule_version: str | None
    rule_status: ApprovalStatus | None
    is_scenario: bool


class VisaRoadmapLinkBlock(ChatRecord):
    """로드맵 화면으로 이동하는 카드."""

    type: Literal["VISA_ROADMAP_LINK"] = "VISA_ROADMAP_LINK"
    roadmap_id: int = Field(ge=1)
    step_count: int = Field(ge=0)
    applicable_from: date | None = Field(description="신청 가능일. 판정할 수 없으면 null")
    rule_status: ApprovalStatus | None


class MissingField(ChatRecord):
    """추가로 입력받을 항목 하나."""

    field: str
    question: str


class MissingInputsBlock(ChatRecord):
    """판정에 필요한데 없는 입력 목록 카드(사용자에게 다시 물을 항목)."""

    type: Literal["MISSING_INPUTS"] = "MISSING_INPUTS"
    fields: tuple[MissingField, ...] = Field(min_length=1)


VisaBlock = Annotated[
    E74ScoreCardBlock | VisaRoadmapLinkBlock | MissingInputsBlock, Field(discriminator="type")
]


class VisaToolRecords(ChatRecord):
    """도구 결과 하나로 만든 채팅용 값: LLM 요약, 결과 참조, 근거, 화면 카드."""

    llm_output: dict[str, Any] = Field(description="LLM에 tool_result로 돌려줄 요약(JSON)")
    result_ref: ResultRef | None
    sources: tuple[SourceRef, ...]
    blocks: tuple[VisaBlock, ...]


# --- 4. 도구 결과 → 채팅 기록 -----------------------------------------------------------


def _missing_block(missing: Sequence[Any]) -> tuple[MissingInputsBlock, ...]:
    if not missing:
        return ()
    fields = tuple(MissingField(field=m.field, question=m.question) for m in missing)
    return (MissingInputsBlock(fields=fields),)


def e74_records(response: E74AssessmentToolResponse) -> VisaToolRecords:
    """E-7-4 평가 결과 → LLM 요약, 결과 참조, 근거, 점수 카드(+ 재질의 카드)."""
    result = response.result
    data = result.model_dump(mode="json", by_alias=True)
    requirements = [
        {"ruleCode": i.rule_code, "status": i.status.value, "messageCode": i.message_code}
        for i in result.items
        if i.category in (RuleCategory.REQUIREMENT, RuleCategory.EXCLUSION)
        and i.status is not ItemStatus.NOT_APPLICABLE
    ]
    llm_output = {
        "assessmentId": response.assessment_id,
        "status": data["status"],
        "ruleVersion": data["ruleVersion"],
        "ruleStatus": data["ruleStatus"],
        "asOfDate": data["asOfDate"],
        "isScenario": data["isScenario"],
        "score": data["score"],
        "potentialScore": data["potentialScore"],
        "scoreGap": data["scoreGap"],
        "koreanDeferralApplied": data["koreanDeferralApplied"],
        "requirements": requirements,
        "missingInputs": data["missingInputs"],
        "needsEvidence": data["needsEvidence"],
        "improvements": data["improvements"][:TOP_IMPROVEMENTS],
        "planToRequired": data["planToRequired"],
        "reviewNotes": data["reviewNotes"],
    }
    card = E74ScoreCardBlock(
        assessment_id=response.assessment_id,
        status=result.status,
        total=result.score.total,
        potential_total=result.potential_score.total,
        required=result.score.required,
        rule_version=result.rule_version,
        rule_status=result.rule_status,
        is_scenario=result.is_scenario,
    )
    return VisaToolRecords(
        llm_output=llm_output,
        result_ref=ResultRef(type=ResultRefType.E74_ASSESSMENT, id=response.assessment_id),
        sources=(SourceRef(type=SourceType.E74_ASSESSMENT, id=response.assessment_id),),
        blocks=(card, *_missing_block(result.missing_inputs)),
    )


def roadmap_records(response: RoadmapToolResponse) -> VisaToolRecords:
    """로드맵 결과 → LLM 요약, 결과 참조, 근거, 로드맵 카드(+ 재질의 카드)."""
    roadmap = response.roadmap
    data = roadmap.model_dump(mode="json", by_alias=True)
    steps = [
        {
            key: step[key]
            for key in (
                "order",
                "kind",
                "title",
                "description",
                "required",
                "actor",
                "estimatedAdditionalScore",
                "targetDate",
                "requiredEvidence",
            )
        }
        for step in data["steps"]
    ]
    llm_output = {
        "roadmapId": response.roadmap_id,
        "goalVisaCode": data["goalVisaCode"],
        "asOfDate": data["asOfDate"],
        "ruleVersion": data["ruleVersion"],
        "ruleStatus": data["ruleStatus"],
        "stay": data["stay"],
        "score": data["score"],
        "steps": steps,
        "projectedScore": data["projectedScore"],
        "missingInputs": data["missingInputs"],
        "warnings": data["warnings"],
        "isMock": data["isMock"],
    }
    card = VisaRoadmapLinkBlock(
        roadmap_id=response.roadmap_id,
        step_count=len(roadmap.steps),
        applicable_from=roadmap.stay.applicable_from,
        rule_status=roadmap.rule_status,
    )
    return VisaToolRecords(
        llm_output=llm_output,
        result_ref=ResultRef(type=ResultRefType.VISA_ROADMAP, id=response.roadmap_id),
        sources=(SourceRef(type=SourceType.VISA_ROADMAP, id=response.roadmap_id),),
        blocks=(card, *_missing_block(roadmap.missing_inputs)),
    )


def policy_search_records(hits: Sequence[SearchHit]) -> VisaToolRecords:
    """정책 검색 결과 → LLM 요약과 근거 후보. 저장 행이 없어 결과 참조와 카드는 없다.

    근거는 실제 검색 결과의 문서 id·청크 id만 쓴다. 답변에 실제로 인용한 것만 남기는 일은
    채팅 쪽에서 한다.
    """
    results = [
        {
            "rank": rank,
            "policyDocumentId": hit.policy_document_id,
            "documentVersion": hit.document_version,
            "chunkId": hit.chunk_id,
            "title": hit.title,
            "sourceOrg": hit.source_org,
            "sourceUrl": hit.source_url,
            "pageNumber": hit.page_number,
            "sectionTitle": hit.section_title,
            "score": round(hit.score, 4),
            "text": hit.chunk_text[:SEARCH_TEXT_CHARS],
        }
        for rank, hit in enumerate(hits, start=1)
    ]
    sources: list[SourceRef] = []
    seen: set[tuple[int, str]] = set()
    for hit in hits:
        key = (hit.policy_document_id, hit.chunk_id)
        if key in seen:
            continue
        seen.add(key)
        sources.append(
            SourceRef(
                type=SourceType.POLICY_DOCUMENT,
                id=hit.policy_document_id,
                chunk_ref=hit.chunk_id or None,
                page_label=f"p.{hit.page_number}" if hit.page_number is not None else None,
            )
        )
    return VisaToolRecords(
        llm_output={"results": results},
        result_ref=None,
        sources=tuple(sources),
        blocks=(),
    )

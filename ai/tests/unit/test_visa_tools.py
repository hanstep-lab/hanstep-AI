"""채팅용 비자 도구 형식(app.visa.tools) 단위 테스트. LLM·Spring·DB 없이 실행한다."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.rag.retrieval import SearchHit
from app.visa.e74.schemas import E74CalculateRequest
from app.visa.e74.service import calculate
from app.visa.tools import (
    CALCULATE_E74_SCORE,
    GET_VISA_ROADMAP,
    SEARCH_VISA_POLICIES,
    CalculateE74ScoreInput,
    E74AssessmentToolResponse,
    E74ScoreCardBlock,
    MissingInputsBlock,
    RoadmapToolResponse,
    SearchVisaPoliciesInput,
    VisaRoadmapLinkBlock,
    VisaToolRecords,
    e74_records,
    policy_search_records,
    roadmap_records,
    tool_definitions,
)
from tests.unit._e74_builders import AS_OF
from tests.unit._e74_cases import profile_json

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "visa_tools"


def _fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data


def _hit(doc_id: int, chunk_id: str, page: int | None, score: float = 0.5) -> SearchHit:
    return SearchHit(
        policy_document_id=doc_id,
        document_version=f"V{doc_id}",
        chunk_index=0,
        chunk_text="본문" * 1000,
        title=f"문서{doc_id}",
        source_url="https://example.org",
        source_org="법무부",
        visa_types=["E-7-4"],
        score=score,
        chunk_id=chunk_id,
        page_number=page,
    )


# --- 도구 정의와 입력 -----------------------------------------------------------------------


def test_tool_definitions_have_closed_camel_case_schemas_without_user_id() -> None:
    definitions = {d["name"]: d for d in tool_definitions()}
    assert list(definitions) == [SEARCH_VISA_POLICIES, CALCULATE_E74_SCORE, GET_VISA_ROADMAP]
    for d in definitions.values():
        schema = d["input_schema"]
        assert d["description"]
        assert schema["additionalProperties"] is False
        # 사용자 식별값은 LLM 인자가 아니다(실행하는 쪽이 넣는다).
        assert not {"userId", "user_id", "sessionId"} & set(schema["properties"])
    assert definitions[SEARCH_VISA_POLICIES]["input_schema"]["required"] == ["query"]
    assert set(definitions[CALCULATE_E74_SCORE]["input_schema"]["properties"]) == {
        "asOfDate",
        "scenario",
        "legalDeclaration",
    }


def test_tool_inputs_accept_camel_case_and_reject_unknown_fields() -> None:
    parsed = CalculateE74ScoreInput.model_validate(
        {
            "asOfDate": "2026-10-25",
            "scenario": {"addEvidences": [{"evidenceType": "TOPIK", "levelValue": "4"}]},
            "legalDeclaration": {"finesUnder1mCount": 0, "hasFine1mOrMore": False},
        }
    )
    assert parsed.scenario is not None and parsed.legal_declaration is not None
    with pytest.raises(ValidationError):
        CalculateE74ScoreInput.model_validate({"userId": 3})
    with pytest.raises(ValidationError):
        SearchVisaPoliciesInput.model_validate({"query": "추천", "top_k": 3})  # snake_case 거부
    with pytest.raises(ValidationError):
        SearchVisaPoliciesInput.model_validate({"query": "추천", "topK": 9})  # 상한 8


# --- Spring 응답 샘플 → 채팅 기록 ------------------------------------------------------------


def test_e74_sample_builds_card_ref_and_source_from_tool_result() -> None:
    response = E74AssessmentToolResponse.model_validate(
        _fixture("e74_assessment_tool_response.json")
    )
    records = e74_records(response)

    assert records.result_ref is not None
    assert (records.result_ref.type, records.result_ref.id) == ("E74_ASSESSMENT", 77)
    assert [(s.type, s.id) for s in records.sources] == [("E74_ASSESSMENT", 77)]
    card = records.blocks[0]
    assert isinstance(card, E74ScoreCardBlock)
    # 카드 숫자는 도구 결과에서만 온다.
    assert card.total == response.result.score.total == 305
    assert card.required == response.result.score.required
    assert card.rule_status == response.result.rule_status
    assert len(records.blocks) == 1  # 재질의할 항목 없음
    assert records.llm_output["assessmentId"] == 77
    assert len(records.llm_output["improvements"]) <= 8
    assert all(r["status"] != "NOT_APPLICABLE" for r in records.llm_output["requirements"])


def test_e74_missing_inputs_add_a_missing_inputs_card() -> None:
    request = E74CalculateRequest.model_validate(
        {"asOfDate": AS_OF.isoformat(), "profile": profile_json("P02")}
    )
    result = calculate(request, include_draft=True)
    assert result.missing_inputs  # 최소 입력 샘플이라 재질의 항목이 있다

    records = e74_records(E74AssessmentToolResponse(assessment_id=5, reused=True, result=result))

    missing = records.blocks[-1]
    assert isinstance(missing, MissingInputsBlock)
    assert [f.field for f in missing.fields] == [m.field for m in result.missing_inputs]


def test_roadmap_sample_builds_link_card_ref_and_source() -> None:
    response = RoadmapToolResponse.model_validate(_fixture("roadmap_tool_response.json"))
    records = roadmap_records(response)

    assert records.result_ref is not None
    assert (records.result_ref.type, records.result_ref.id) == ("VISA_ROADMAP", 31)
    assert [(s.type, s.id) for s in records.sources] == [("VISA_ROADMAP", 31)]
    card = records.blocks[0]
    assert isinstance(card, VisaRoadmapLinkBlock)
    assert card.step_count == len(response.roadmap.steps)
    assert card.applicable_from == response.roadmap.stay.applicable_from
    assert [s["order"] for s in records.llm_output["steps"]] == [
        s.order for s in response.roadmap.steps
    ]


def test_policy_search_sources_come_only_from_hits_and_are_deduplicated() -> None:
    hits = [_hit(6, "c1", 12, 0.91234), _hit(6, "c1", 12), _hit(3, "c9", None)]
    records = policy_search_records(hits)

    assert records.result_ref is None and records.blocks == ()
    assert [(s.id, s.chunk_ref, s.page_label) for s in records.sources] == [
        (6, "c1", "p.12"),
        (3, "c9", None),
    ]
    first = records.llm_output["results"][0]
    assert (first["rank"], first["score"], len(first["text"])) == (1, 0.9123, 1200)


def test_records_serialize_to_chat_table_shapes() -> None:
    response = E74AssessmentToolResponse.model_validate(
        _fixture("e74_assessment_tool_response.json")
    )
    data = e74_records(response).model_dump(mode="json", by_alias=True)

    assert set(data) == {"llmOutput", "resultRef", "sources", "blocks"}
    assert data["resultRef"] == {"type": "E74_ASSESSMENT", "id": 77}
    # 근거 키는 agent.sources·Spring CHT-04와 같다(type·id가 없으면 Spring이 버린다).
    assert data["sources"][0] == {
        "type": "E74_ASSESSMENT",
        "id": 77,
        "chunkRef": None,
        "pageLabel": None,
    }
    assert data["blocks"][0]["type"] == "E74_SCORE_CARD"
    # 저장된 값을 다시 읽어도 같은 모양이다(블록은 type으로 구분).
    assert VisaToolRecords.model_validate(data).blocks[0] == e74_records(response).blocks[0]


def test_unknown_block_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        VisaToolRecords.model_validate(
            {
                "llmOutput": {},
                "resultRef": None,
                "sources": [],
                "blocks": [{"type": "TRANSFER_BUTTON"}],
            }
        )

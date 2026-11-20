"""채팅용 비자 도구 형식(app.visa.tools) 단위 테스트. LLM·Spring·DB 없이 실행한다."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.rag.retrieval import SearchHit
from app.visa.e74.schemas import E74CalculateRequest
from app.visa.e74.service import calculate
from app.visa.tools import (
    CALCULATE_E74_SCORE,
    E74_SCHEMA_DESCRIPTIONS,
    GET_VISA_ROADMAP,
    LEGAL_DECLARATION_ITEMS,
    LEGAL_DECLARATION_KEY,
    SEARCH_VISA_POLICIES,
    CalculateE74ScoreInput,
    E74AssessmentToolResponse,
    E74ScoreCardBlock,
    GetVisaRoadmapInput,
    MissingInputsBlock,
    RoadmapToolResponse,
    SearchVisaPoliciesInput,
    VisaRoadmapLinkBlock,
    VisaToolRecords,
    e74_records,
    policy_search_records,
    recorded_arguments,
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


def _descriptions(node: Any, path: str = "") -> dict[str, str]:
    """JSON 스키마 안의 설명(description 문자열)을 위치별로 모은다."""
    if isinstance(node, list):
        return {
            k: v for i, x in enumerate(node) for k, v in _descriptions(x, f"{path}[{i}]").items()
        }
    if not isinstance(node, dict):
        return {}
    found = {path: node["description"]} if isinstance(node.get("description"), str) else {}
    for key, value in node.items():
        found |= _descriptions(value, f"{path}.{key}" if path else key)
    return found


def test_e74_tool_definition_is_english_and_keeps_the_two_instructions() -> None:
    """LLM에 보이는 calculate_e74_score 설명은 영어이고 개발자 메모가 없다(#108)."""
    (e74,) = [d for d in tool_definitions() if d["name"] == CALCULATE_E74_SCORE]
    texts = [e74["description"], *_descriptions(e74["input_schema"]).values()]
    assert not [t for t in texts if re.search(r"[가-힣]", t)]
    assert not [t for t in texts if re.search(r"MySQL|to_camel|Jackson|visa_evidence", t)]
    # 스키마 설명은 E74_SCHEMA_DESCRIPTIONS 문구뿐이고, 사전의 위치는 모두 스키마에 있다.
    assert _descriptions(e74["input_schema"]) == E74_SCHEMA_DESCRIPTIONS
    # 지시 두 가지: 점수 질문에는 반드시 부른다, 물어서 받은 답만 넣는다
    assert "Always call it for questions about the user's own E-7-4 score" in e74["description"]
    legal = e74["input_schema"]["properties"]["legalDeclaration"]["description"]
    assert "include only answers the user gave when asked" in legal
    # 모양(인자 이름·타입·제약)은 그대로다.
    raw = CalculateE74ScoreInput.model_json_schema(by_alias=True)
    assert _without_descriptions(e74["input_schema"]) == _without_descriptions(
        {k: v for k, v in raw.items() if k not in ("title",)}
    )


def _without_descriptions(node: Any) -> Any:
    if isinstance(node, list):
        return [_without_descriptions(v) for v in node]
    if isinstance(node, dict):
        return {
            k: _without_descriptions(v)
            for k, v in node.items()
            if not (k == "description" and isinstance(v, str))
        }
    return node


def test_other_tool_definitions_are_unchanged() -> None:
    definitions = {d["name"]: d for d in tool_definitions()}
    for name in (SEARCH_VISA_POLICIES, GET_VISA_ROADMAP):
        model = SearchVisaPoliciesInput if name == SEARCH_VISA_POLICIES else GetVisaRoadmapInput
        assert definitions[name]["description"] == model.__doc__


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


def test_recorded_arguments_keep_only_which_legal_items_were_answered() -> None:
    arguments: dict[str, Any] = {
        "asOfDate": "2026-10-25",
        "legalDeclaration": {
            "finesUnder1mCount": 2,
            "hasFine1mOrMore": False,
            "hasCurrentTaxArrears": None,
        },
    }
    recorded = recorded_arguments(arguments)

    assert recorded == {
        "asOfDate": "2026-10-25",
        "legalDeclaration": {"answered": ["finesUnder1mCount", "hasFine1mOrMore"]},
    }
    # 도구 실행에 쓰는 원래 인자는 바뀌지 않는다.
    assert arguments["legalDeclaration"]["finesUnder1mCount"] == 2
    # 문항 이름은 LLM 도구 스키마의 이름과 같다.
    schema = CalculateE74ScoreInput.model_json_schema(by_alias=True)
    assert LEGAL_DECLARATION_KEY in schema["properties"]
    assert list(LEGAL_DECLARATION_ITEMS) == list(
        schema["$defs"]["LegalDeclarationIn"]["properties"]
    )


@pytest.mark.parametrize(
    ("declaration", "answered"),
    [
        ("벌금 2회", []),
        (["finesUnder1mCount", 2], []),
        ({"finesUnder1mCount": 1, "unknownItem": "detail"}, ["finesUnder1mCount"]),
    ],
)
def test_recorded_arguments_drop_malformed_legal_values(
    declaration: Any, answered: list[str]
) -> None:
    # LLM이 모양을 틀리거나 다른 도구 인자에 넣어도 값·모르는 키는 남지 않는다.
    recorded = recorded_arguments({"query": "E-7-4", "legalDeclaration": declaration})

    assert recorded == {"query": "E-7-4", "legalDeclaration": {"answered": answered}}


@pytest.mark.parametrize(
    ("arguments", "recorded"),
    [
        # snake_case 키: 도구는 거부(REJECTED)하지만 Spring은 인자를 저장한다.
        (
            {"legal_declaration": {"fines_under_1m_count": 2, "has_fine_1m_or_more": False}},
            {"legal_declaration": {"answered": ["finesUnder1mCount", "hasFine1mOrMore"]}},
        ),
        # 다른 인자 안에 넣은 경우
        (
            {"scenario": {"legalDeclaration": {"taxStayRestrictionCount": 1}}},
            {"scenario": {"legalDeclaration": {"answered": ["taxStayRestrictionCount"]}}},
        ),
        # 문항 이름을 바로 인자로 쓴 경우(대소문자·밑줄 무시)
        (
            {"asOfDate": "2026-11-12", "FINES_UNDER_1M_COUNT": 3, "hasCurrentTaxArrears": True},
            {
                "asOfDate": "2026-11-12",
                "FINES_UNDER_1M_COUNT": "REDACTED",
                "hasCurrentTaxArrears": "REDACTED",
            },
        ),
        # 목록 안에 들어 있는 경우
        (
            {"items": [{"legalDeclaration": {"immigrationViolationCount": 2}}, {"query": "q"}]},
            {
                "items": [
                    {"legalDeclaration": {"answered": ["immigrationViolationCount"]}},
                    {"query": "q"},
                ]
            },
        ),
    ],
)
def test_recorded_arguments_hide_legal_values_under_any_key_or_depth(
    arguments: dict[str, Any], recorded: dict[str, Any]
) -> None:
    assert recorded_arguments(arguments) == recorded


def test_recorded_arguments_without_legal_answers_are_unchanged() -> None:
    for arguments in ({"query": "E-7-4", "topK": 3}, {"asOfDate": None, "legalDeclaration": None}):
        assert recorded_arguments(arguments) == arguments


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
    # 카드 question은 영어 문구다(#103).
    assert [f.question for f in missing.fields] == [m.question_en for m in result.missing_inputs]
    assert not any(re.search(r"[가-힣]", f.question) for f in missing.fields)
    # LLM에 주는 도구 결과는 #101에서 잰 모양 그대로이고 question은 카드와 같은 영어다(#108).
    llm_missing = records.llm_output["missingInputs"]
    assert all(set(m) == {"field", "question", "ruleCodes"} for m in llm_missing)
    assert [m["question"] for m in llm_missing] == [f.question for f in missing.fields]
    assert [m["ruleCodes"] for m in llm_missing] == [
        list(m.rule_codes) for m in result.missing_inputs
    ]


def test_missing_inputs_card_finds_english_for_results_saved_before_question_en() -> None:
    """#103 전에 저장돼 재사용된 결과(questionEn 없음)도 카드에는 영어 문구를 쓴다."""
    request = E74CalculateRequest.model_validate(
        {"asOfDate": AS_OF.isoformat(), "profile": profile_json("P02")}
    )
    fresh = calculate(request, include_draft=True)
    saved = fresh.model_dump(mode="json", by_alias=True)
    for m in saved["missingInputs"]:
        del m["questionEn"]
    for item in saved["items"]:
        for m in item["missingInputs"]:
            del m["questionEn"]
    response = E74AssessmentToolResponse.model_validate(
        {"assessmentId": 5, "reused": True, "result": saved}
    )
    assert all(m.question_en is None for m in response.result.missing_inputs)

    missing = e74_records(response).blocks[-1]
    assert isinstance(missing, MissingInputsBlock)
    assert [f.question for f in missing.fields] == [m.question_en for m in fresh.missing_inputs]


HANGUL = re.compile(r"[가-힣]")
LEGAL_ANSWERS = {
    "finesUnder1mCount": 0,
    "hasFine1mOrMore": False,
    "taxStayRestrictionCount": 0,
    "immigrationViolationCount": 0,
    "hasOverstay3mOrMore": False,
    "hasCurrentTaxArrears": False,
}


def _korean_strings(value: Any, path: str = "") -> list[tuple[str, str]]:
    if isinstance(value, dict):
        return [h for k, v in value.items() for h in _korean_strings(v, f"{path}.{k}")]
    if isinstance(value, list):
        return [h for i, v in enumerate(value) for h in _korean_strings(v, f"{path}[{i}]")]
    return [(path, value)] if isinstance(value, str) and HANGUL.search(value) else []


def _user_inputs(body: dict[str, Any]) -> list[str]:
    """사용자가 입력한 원문(증빙 값·사업장명). LLM 결과에 그대로 들어가도 되는 한국어다."""
    return [
        *(e["levelValue"] for e in body.get("visaEvidences", []) if e.get("levelValue")),
        *(h["employerName"] for h in body.get("employmentHistories", []) if h.get("employerName")),
    ]


def _e74_cases() -> list[tuple[str, dict[str, Any], str]]:
    cases = []
    for case_id in ("P00", "P01", "P02", "P03", "P04", "P05"):
        body = profile_json(case_id)
        for as_of in ("2026-10-25", "2026-11-19", "2026-12-05"):
            cases.append((f"{case_id}@{as_of}", body, as_of))
            cases.append(
                (f"{case_id}+legal@{as_of}", {**body, "legalDeclaration": LEGAL_ANSWERS}, as_of)
            )
    # 검토 메모가 붙는 경우: 최근 2년 소득이 없어 대체 연도 사용, 19세 미만
    p01 = profile_json("P01")
    income = [
        {"incomeYear": y, "annualAmount": a, "source": "DOCUMENT", "verificationStatus": "VERIFIED"}
        for y, a in ((2022, 31000000), (2023, 32000000))
    ]
    cases.append(("P01-income-fallback", {**p01, "incomeRecords": income}, "2026-11-19"))
    young = {**p01, "visaProfile": {**p01["visaProfile"], "birthDate": "2008-01-01"}}
    cases.append(("P01-under-19", young, "2026-11-19"))
    return cases


E74_CASES = _e74_cases()


def _calculate(body: dict[str, Any], as_of: str) -> Any:
    request = E74CalculateRequest.model_validate({"asOfDate": as_of, "profile": body})
    return calculate(request, include_draft=True)


@pytest.mark.parametrize(("label", "body", "as_of"), E74_CASES, ids=[c[0] for c in E74_CASES])
def test_e74_llm_output_has_no_korean_except_user_input(
    label: str, body: dict[str, Any], as_of: str
) -> None:
    """LLM에 주는 E-7-4 도구 결과에는 한글이 없다(#108).

    사용자 입력 원문(자격증 이름 등)만 예외다.
    """
    records = e74_records(
        E74AssessmentToolResponse(assessment_id=5, reused=False, result=_calculate(body, as_of))
    )
    leftover = []
    for path, text in _korean_strings(records.llm_output):
        for value in _user_inputs(body):
            text = text.replace(value, "")
        if HANGUL.search(text):
            leftover.append((path, text))
    assert leftover == []


@pytest.mark.parametrize(("label", "body", "as_of"), E74_CASES, ids=[c[0] for c in E74_CASES])
def test_e74_llm_output_is_the_same_for_results_saved_before_english(
    label: str, body: dict[str, Any], as_of: str
) -> None:
    """영어 필드가 없던 저장 결과(#103·#108 전, 재사용된 평가)도 LLM에는 같은 영어 결과를 준다."""
    fresh = _calculate(body, as_of)
    saved = fresh.model_dump(mode="json", by_alias=True)
    for imp in saved["improvements"]:
        del imp["descriptionEn"], imp["documentsEn"]
    for m in saved["missingInputs"]:
        del m["questionEn"]
    old = E74AssessmentToolResponse.model_validate(
        {"assessmentId": 5, "reused": True, "result": saved}
    )
    assert all(i.description_en is None for i in old.result.improvements)

    new = E74AssessmentToolResponse(assessment_id=5, reused=True, result=fresh)
    for language in ("en", "ko"):
        assert e74_records(old, language).llm_output == e74_records(new, language).llm_output


def test_e74_llm_output_keeps_the_shape_and_uses_english_texts() -> None:
    result = _calculate(profile_json("P02"), "2026-11-19")
    llm = e74_records(E74AssessmentToolResponse(assessment_id=5, reused=False, result=result))
    output = llm.llm_output

    assert "reviewNotes" not in output and output["reviewNotices"] == []
    first, imp = output["improvements"][0], result.improvements[0]
    # 개선 방안 모양은 그대로(descriptionEn·documentsEn 키 없음)이고 값만 영어다.
    assert list(first) == [
        "ruleCode",
        "kind",
        "actor",
        "pointsGain",
        "blocking",
        "availableFrom",
        "target",
        "documents",
        "messageCode",
        "description",
    ]
    assert (first["description"], first["documents"]) == (
        imp.description_en,
        list(imp.documents_en or ()),
    )
    # 엔진 결과(Spring 저장·로드맵 화면)의 한국어는 그대로다.
    assert HANGUL.search(imp.description) and all(HANGUL.search(d) for d in imp.documents)


def test_e74_review_notes_become_english_notices_for_the_llm() -> None:
    """검토 메모(사람 확인용 한국어)는 LLM에 주지 않고 안내 코드·영어로 준다. 같은 안내는 합친다."""
    label, body, as_of = E74_CASES[-2]
    assert label == "P01-income-fallback"
    result = _calculate(body, as_of)
    assert [n.rule_code for n in result.review_notes] == ["INCOME_30_35", "REQ_MIN_INCOME"]

    output = e74_records(
        E74AssessmentToolResponse(assessment_id=5, reused=False, result=result)
    ).llm_output
    assert output["reviewNotices"] == [
        {
            "code": "INCOME_FROM_EARLIER_YEARS",
            "ruleCodes": ["INCOME_30_35", "REQ_MIN_INCOME"],
            "text": "Income for the last 2 years is missing, so the highest years from 2021 to"
            " 2025 were used. The reason, such as a gap after re-entry, needs a manual check",
        }
    ]
    assert not _korean_strings(output["reviewNotices"])


def test_e74_sample_saved_before_english_gives_english_llm_output() -> None:
    """Spring 평가 응답 샘플(영어 필드 없음)도 LLM 결과에 한글이 없다."""
    response = E74AssessmentToolResponse.model_validate(
        _fixture("e74_assessment_tool_response.json")
    )
    assert all(i.description_en is None for i in response.result.improvements)
    output = e74_records(response).llm_output
    assert output["improvements"] and _korean_strings(output) == []
    assert all(i["description"] and i["documents"] for i in output["improvements"])


# --- ko 사용자: 엔진 한국어 문구, 내부 표기 없음 ------------------------------------------

# 내부 표기: 확인 번호(Q8·Q9), 양식 번호("(붙임5)"), 사람 검토 표시. ko·en 모두 LLM에 주지 않는다.
INTERNAL_MARKS = re.compile(r"\bQ\d+\b|붙임|검토 필요|확인 필요")
TEXT_KEYS = ("question", "description", "documents", "text")


def _strings(value: Any, path: str = "") -> list[tuple[str, str]]:
    if isinstance(value, dict):
        return [h for k, v in value.items() for h in _strings(v, f"{path}.{k}")]
    if isinstance(value, list):
        return [h for i, v in enumerate(value) for h in _strings(v, f"{path}[{i}]")]
    return [(path, value)] if isinstance(value, str) else []


def _without_texts(value: Any) -> Any:
    """문구(질문·설명·서류·안내)를 뺀 모양. 언어가 달라도 같아야 한다."""
    if isinstance(value, dict):
        return {k: _without_texts(v) for k, v in value.items() if k not in TEXT_KEYS}
    if isinstance(value, list):
        return [_without_texts(v) for v in value]
    return value


@pytest.mark.parametrize(("label", "body", "as_of"), E74_CASES, ids=[c[0] for c in E74_CASES])
def test_e74_korean_llm_output_has_no_internal_marks_and_keeps_the_shape(
    label: str, body: dict[str, Any], as_of: str
) -> None:
    result = _calculate(body, as_of)
    response = E74AssessmentToolResponse(assessment_id=5, reused=False, result=result)
    ko, en = e74_records(response, "ko"), e74_records(response, "en")

    assert [(p, t) for p, t in _strings(ko.llm_output) if INTERNAL_MARKS.search(t)] == []
    assert [(p, t) for p, t in _strings(en.llm_output) if INTERNAL_MARKS.search(t)] == []
    # 숫자·코드·모양은 언어와 상관없다. 점수 카드도 같다.
    assert _without_texts(ko.llm_output) == _without_texts(en.llm_output)
    assert ko.blocks[0] == en.blocks[0]
    # 질문은 엔진 한국어 문구 그대로다. 재질의 카드도 같은 한국어 질문이다.
    korean = [m.question for m in result.missing_inputs]
    assert [m["question"] for m in ko.llm_output["missingInputs"]] == korean
    if result.missing_inputs:
        ko_card, en_card = ko.blocks[-1], en.blocks[-1]
        assert isinstance(ko_card, MissingInputsBlock) and isinstance(en_card, MissingInputsBlock)
        assert [f.field for f in ko_card.fields] == [f.field for f in en_card.fields]
        assert [f.question for f in ko_card.fields] == korean
        assert all(HANGUL.search(q) for q in korean)
        assert [f.question for f in en_card.fields] == [
            m.question_en for m in result.missing_inputs
        ]
    else:
        assert len(ko.blocks) == len(en.blocks) == 1


def test_e74_korean_llm_output_drops_internal_marks_from_rule_and_document_texts() -> None:
    """P03: 엔진 한국어에는 "(Q8, 검토 필요)"·"(붙임5)"가 있고, ko LLM 결과에는 없다."""
    result = _calculate(profile_json("P03"), "2026-11-19")
    output = e74_records(
        E74AssessmentToolResponse(assessment_id=5, reused=False, result=result), "ko"
    ).llm_output
    engine = {(i.rule_code, i.message_code): i for i in result.improvements}
    llm = {(i["ruleCode"], i["messageCode"]): i for i in output["improvements"]}

    stay = ("REQ_STAY_PERIOD", "E74_IMPROVE_SUBMIT_REQUIREMENT_EVIDENCE")
    assert "(Q8, 검토 필요)" in engine[stay].description
    assert llm[stay]["description"] == engine[stay].description.replace("(Q8, 검토 필요)", "")
    employer = ("REC_EMPLOYER", "E74_IMPROVE_SUBMIT_EVIDENCE")
    assert "고용기업 추천서(붙임5)" in engine[employer].documents
    assert llm[employer]["documents"] == ["고용기업 추천서(공식 양식)", "추천자 신분증 사본"]


def test_e74_review_notes_become_korean_notices_for_korean_users() -> None:
    """ko 사용자도 검토 메모 원문은 주지 않고 같은 코드의 한국어 안내를 준다."""
    label, body, as_of = E74_CASES[-2]
    assert label == "P01-income-fallback"
    result = _calculate(body, as_of)
    output = e74_records(
        E74AssessmentToolResponse(assessment_id=5, reused=False, result=result), "ko"
    ).llm_output
    assert output["reviewNotices"] == [
        {
            "code": "INCOME_FROM_EARLIER_YEARS",
            "ruleCodes": ["INCOME_30_35", "REQ_MIN_INCOME"],
            "text": "최근 2년 소득이 없어 2021~2025년 중 높은 해로 계산함. "
            "재입국 공백 같은 사유는 따로 확인해야 함",
        }
    ]
    assert output["reviewNotices"][0]["text"] != result.review_notes[0].note


@pytest.mark.parametrize("language", ["vi", "zh"])
def test_e74_other_languages_get_the_english_llm_output_and_card(language: str) -> None:
    """vi·zh는 고려하지 않는 언어라 en과 같은 영어 결과·카드를 준다(측정하지 않음)."""
    result = _calculate(profile_json("P02"), "2026-11-19")
    response = E74AssessmentToolResponse(assessment_id=5, reused=False, result=result)
    other, en = e74_records(response, language), e74_records(response, "en")
    assert other.llm_output == en.llm_output
    assert other.blocks == en.blocks


def test_e74_korean_card_uses_korean_questions_for_results_saved_before_question_en() -> None:
    """#103 전 저장 결과(questionEn 없음)도 ko 카드는 원래 한국어 질문을 그대로 쓴다."""
    fresh = _calculate(profile_json("P02"), "2026-11-19")
    saved = fresh.model_dump(mode="json", by_alias=True)
    for m in saved["missingInputs"]:
        del m["questionEn"]
    old = E74AssessmentToolResponse.model_validate(
        {"assessmentId": 5, "reused": True, "result": saved}
    )
    card = e74_records(old, "ko").blocks[-1]
    assert isinstance(card, MissingInputsBlock)
    assert [f.question for f in card.fields] == [m.question for m in fresh.missing_inputs]


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

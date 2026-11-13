"""챗봇 비자 도구(app.chat.visa_tools)와 도구 묶음 단위 테스트. Spring·MongoDB·LLM 없이 실행한다."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.messages import AIMessageChunk, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk

from app.chat.fake_llm import FAKE_MODEL_ID, FakeChatModel
from app.chat.schemas import ChatStreamRequest, LlmConfig
from app.chat.stream import ChatRunOptions, run_chat
from app.chat.tools import TOOL_NAME, ChatToolset, FinanceTool, FixtureFinanceContext
from app.chat.visa_tools import VisaTools, kst_today
from app.core.config import Settings
from app.integrations.policy_gateway import UnconfiguredPolicyGateway
from app.integrations.spring_client import SpringApiError, SpringInternalClient
from app.main import AppState, create_app
from app.rag.embeddings import BgeM3Embedder
from app.rag.errors import ValidationFailedError
from app.rag.retrieval import SearchHit, SearchUnavailableError
from app.visa.tools import (
    CALCULATE_E74_SCORE,
    SEARCH_VISA_POLICIES,
    E74AssessmentToolRequest,
    E74AssessmentToolResponse,
)

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "visa_tools"
LEGAL = {"finesUnder1mCount": 2, "hasFine1mOrMore": False}


def _response() -> E74AssessmentToolResponse:
    """Spring 평가 응답 샘플(P00, assessmentId 77, 확정 305점)."""
    data = json.loads((FIXTURE / "e74_assessment_tool_response.json").read_text(encoding="utf-8"))
    return E74AssessmentToolResponse.model_validate(data)


class FakeE74Source:
    """받은 요청을 기록하고 정해 둔 응답이나 오류를 돌려주는 가짜 Spring 평가 API."""

    def __init__(self, error: BaseException | None = None, delay: float = 0) -> None:
        self.calls: list[tuple[int, E74AssessmentToolRequest, str | None]] = []
        self.error = error
        self.delay = delay

    async def assess(
        self, user_id: int, request: E74AssessmentToolRequest, request_id: str | None
    ) -> E74AssessmentToolResponse:
        self.calls.append((user_id, request, request_id))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return _response()


def _hit(doc_id: int, chunk_id: str, page: int | None) -> SearchHit:
    return SearchHit(
        policy_document_id=doc_id,
        document_version=f"V{doc_id}",
        chunk_index=0,
        chunk_text="현재 1년 이상 근무 중인 기업의 추천",
        title="2026년 숙련기능인력(E-7-4) 선발 계획",
        source_url="https://example.org",
        source_org="법무부",
        visa_types=["E-7-4"],
        score=0.85,
        chunk_id=chunk_id,
        page_number=page,
    )


class FakeSearch:
    """정해 둔 검색 결과나 오류를 돌려주는 가짜 운영 검색."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.calls: list[tuple[str, int, list[str] | None]] = []
        self.error = error

    async def search(self, query: str, top_k: int, visa_types: list[str] | None) -> list[SearchHit]:
        self.calls.append((query, top_k, visa_types))
        if self.error is not None:
            raise self.error
        return [_hit(6, "c1", 3), _hit(6, "c1", 3), _hit(3, "c9", None)]


def _tools(e74: Any = None, search: Any = None, timeout: float = 5) -> VisaTools:
    return VisaTools(
        e74_source=e74, search_source=search, user_id=185, request_id="req_v", timeout=timeout
    )


# --- 도구 묶음 ------------------------------------------------------------------------------


def test_toolset_offers_finance_and_visa_tools_and_rejects_unknown_names() -> None:
    finance = FinanceTool(FixtureFinanceContext(), user_id=1, request_id=None, timeout=5)
    toolset = ChatToolset(finance, _tools())

    assert [s["name"] for s in toolset.schemas] == [
        TOOL_NAME,
        SEARCH_VISA_POLICIES,
        CALCULATE_E74_SCORE,
    ]
    outcome = asyncio.run(toolset.run("send_money", {"amount": 1}))
    assert (outcome.status, outcome.error_code, outcome.arguments) == (
        "REJECTED",
        "UNKNOWN_TOOL",
        {},
    )
    with pytest.raises(ValueError, match="duplicate"):
        ChatToolset(finance, finance)


# --- calculate_e74_score --------------------------------------------------------------------


def test_calculate_returns_summary_ref_source_and_score_card() -> None:
    source = FakeE74Source()
    arguments = {"asOfDate": "2026-11-12", "legalDeclaration": LEGAL}

    outcome = asyncio.run(_tools(source).run(CALCULATE_E74_SCORE, arguments))

    assert outcome.status == "SUCCEEDED"
    # 사용자 id·요청 id는 요청에 고정된 값이다. 감점 답은 도구 실행에는 그대로 간다.
    user_id, request, request_id = source.calls[0]
    assert (user_id, request_id, request.as_of_date) == (185, "req_v", date(2026, 11, 12))
    assert request.legal_declaration is not None
    assert request.legal_declaration.fines_under_1m_count == 2
    # 채팅 기록에는 감점 답 값 대신 답한 문항 이름만 남는다.
    assert outcome.arguments == {
        "asOfDate": "2026-11-12",
        "legalDeclaration": {"answered": ["finesUnder1mCount", "hasFine1mOrMore"]},
    }
    assert json.loads(outcome.content)["assessmentId"] == 77
    assert outcome.result_ref == {"type": "E74_ASSESSMENT", "id": 77}
    assert outcome.sources == [
        {"type": "E74_ASSESSMENT", "id": 77, "chunkRef": None, "pageLabel": None}
    ]
    card = outcome.blocks[0]
    assert (card["type"], card["assessmentId"], card["total"]) == ("E74_SCORE_CARD", 77, 305)


def test_calculate_defaults_to_kst_today() -> None:
    source = FakeE74Source()
    asyncio.run(_tools(source).run(CALCULATE_E74_SCORE, {}))
    assert source.calls[0][1].as_of_date == kst_today()


@pytest.mark.parametrize(
    ("arguments", "field"),
    [({"userId": 3}, "userId"), ({"asOfDate": "내일"}, "asOfDate")],
)
def test_calculate_rejects_invalid_arguments_without_calling_spring(
    arguments: dict[str, Any], field: str
) -> None:
    source = FakeE74Source()
    outcome = asyncio.run(_tools(source).run(CALCULATE_E74_SCORE, arguments))

    assert (outcome.status, outcome.error_code) == ("REJECTED", "INVALID_ARGUMENTS")
    assert field in outcome.content
    assert source.calls == []


@pytest.mark.parametrize(
    ("error", "status", "code", "content"),
    [
        (
            SpringApiError(422, "INSUFFICIENT_DATA", "", "NO_VISA_PROFILE"),
            "FAILED",
            "SPRING_HTTP_422",
            "no saved visa profile",
        ),
        (
            SpringApiError(409, "INVALID_STATE", "", "RULE_SET_NOT_APPROVED"),
            "FAILED",
            "SPRING_HTTP_409",
            "not approved yet",
        ),
        # 입력·저장 데이터 문제(4xx)는 REJECTED: 코드와 필드 경로·사유를 넘긴다(값은 없음).
        (
            SpringApiError(
                400,
                "VALIDATION_ERROR",
                "",
                fields=(("profile.legalDeclaration.finesUnder1mCount", "int_parsing"),),
            ),
            "REJECTED",
            "SPRING_HTTP_400",
            "(VALIDATION_ERROR): legalDeclaration.finesUnder1mCount (int_parsing)",
        ),
        (
            SpringApiError(
                422,
                "INSUFFICIENT_DATA",
                "",
                "PROFILE_REJECTED",
                (("profile.employments.1", "value_error"),),
            ),
            "REJECTED",
            "SPRING_HTTP_422",
            "(PROFILE_REJECTED): profile.employments.1 (value_error)",
        ),
        (
            SpringApiError(409, "INVALID_STATE", "", "RULE_SET_NOT_REGISTERED"),
            "REJECTED",
            "SPRING_HTTP_409",
            "RULE_SET_NOT_REGISTERED",
        ),
        # 5xx·시간 초과·연결 오류는 "지금 쓸 수 없음".
        (SpringApiError(500, "INTERNAL_ERROR", ""), "FAILED", "SPRING_HTTP_500", "unavailable"),
        (SpringApiError(502, "AI_UPSTREAM_ERROR", ""), "FAILED", "SPRING_HTTP_502", "unavailable"),
        (httpx.ReadTimeout("slow"), "TIMEOUT", "SPRING_TIMEOUT", "unavailable"),
        (httpx.ConnectError("refused"), "FAILED", "SPRING_ERROR", "unavailable"),
    ],
)
def test_calculate_failures_become_tool_errors(
    error: BaseException, status: str, code: str, content: str
) -> None:
    outcome = asyncio.run(_tools(FakeE74Source(error)).run(CALCULATE_E74_SCORE, {}))
    assert (outcome.status, outcome.error_code) == (status, code)
    assert content in outcome.content
    assert outcome.blocks == [] and outcome.result_ref is None


def test_calculate_timeout_budget_and_missing_spring() -> None:
    slow = asyncio.run(_tools(FakeE74Source(delay=1), timeout=0.01).run(CALCULATE_E74_SCORE, {}))
    assert (slow.status, slow.error_code) == ("TIMEOUT", "SPRING_TIMEOUT")

    missing = asyncio.run(_tools(None).run(CALCULATE_E74_SCORE, {}))
    assert (missing.status, missing.error_code) == ("FAILED", "SPRING_NOT_CONFIGURED")


# --- search_visa_policies -------------------------------------------------------------------


def test_search_returns_results_and_deduplicated_policy_sources() -> None:
    search = FakeSearch()
    outcome = asyncio.run(
        _tools(search=search).run(SEARCH_VISA_POLICIES, {"query": "고용기업 추천", "topK": 3})
    )

    assert search.calls == [("고용기업 추천", 3, None)]
    assert outcome.status == "SUCCEEDED"
    assert [r["policyDocumentId"] for r in json.loads(outcome.content)["results"]] == [6, 6, 3]
    assert outcome.sources == [
        {"type": "POLICY_DOCUMENT", "id": 6, "chunkRef": "c1", "pageLabel": "p.3"},
        {"type": "POLICY_DOCUMENT", "id": 3, "chunkRef": "c9", "pageLabel": None},
    ]
    assert outcome.result_ref is None and outcome.blocks == []


@pytest.mark.parametrize(
    ("search", "status", "code"),
    [
        (None, "FAILED", "SEARCH_NOT_CONFIGURED"),
        (FakeSearch(SearchUnavailableError("approval")), "FAILED", "SEARCH_UNAVAILABLE"),
        (FakeSearch(ValidationFailedError("too many visa_types")), "REJECTED", "INVALID_ARGUMENTS"),
        (FakeSearch(RuntimeError("boom")), "FAILED", "SEARCH_ERROR"),
    ],
)
def test_search_failures_become_tool_errors(search: Any, status: str, code: str) -> None:
    outcome = asyncio.run(_tools(search=search).run(SEARCH_VISA_POLICIES, {"query": "q"}))
    assert (outcome.status, outcome.error_code) == (status, code)


# --- Spring 클라이언트 ------------------------------------------------------------------------


def _spring(handler: Any) -> SpringInternalClient:
    return SpringInternalClient(
        base_url="http://spring.test",
        api_key="s" * 40,
        client=httpx.Client(base_url="http://spring.test", transport=httpx.MockTransport(handler)),
    )


def test_spring_e74_assessment_posts_once_and_reads_reason() -> None:
    seen: list[httpx.Request] = []

    def ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"assessmentId": 1, "reused": True, "result": {}})

    body = {"asOfDate": "2026-11-12", "scenario": None, "legalDeclaration": None}
    data = _spring(ok).e74_assessment(185, body, timeout=5, request_id="req_x")
    assert data["reused"] is True
    assert seen[0].url.path == "/internal/v1/ai-context/users/185/e74-assessments"
    assert seen[0].headers["X-Request-Id"] == "req_x"
    assert json.loads(seen[0].content) == body

    def missing(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        payload = {"code": "INSUFFICIENT_DATA", "details": {"reason": "NO_VISA_PROFILE"}}
        return httpx.Response(422, json=payload)

    seen.clear()
    with pytest.raises(SpringApiError) as info:
        _spring(missing).e74_assessment(1, body, timeout=5)
    assert (info.value.status, info.value.code, info.value.reason) == (
        422,
        "INSUFFICIENT_DATA",
        "NO_VISA_PROFILE",
    )
    assert len(seen) == 1  # 재시도하지 않는다


def test_spring_e74_error_keeps_only_field_paths_and_reasons() -> None:
    def rejected(_: httpx.Request) -> httpx.Response:
        fields = [
            {"field": "scenario.addEvidences.0.evidenceType", "reason": "enum"},
            {"field": "scenario", "reason": "must be an object"},
            {"field": "profile.legalDeclaration.finesUnder1mCount", "reason": "010-1234-5678"},
            {"field": "이름 홍길동", "reason": "missing"},
            "not a field",
        ]
        payload = {"code": "VALIDATION_ERROR", "details": {"fields": fields}}
        return httpx.Response(400, json=payload)

    body = {"asOfDate": "2026-11-12", "scenario": None, "legalDeclaration": None}
    with pytest.raises(SpringApiError) as info:
        _spring(rejected).e74_assessment(185, body, timeout=5)

    # 경로·코드 모양이 아닌 값은 넘기지 않는다(사유는 invalid, 경로가 아니면 항목을 뺀다).
    assert info.value.fields == (
        ("scenario.addEvidences.0.evidenceType", "enum"),
        ("scenario", "must be an object"),
        ("profile.legalDeclaration.finesUnder1mCount", "invalid"),
    )


# --- 그래프·스트림까지 -------------------------------------------------------------------------


class E74ToolModel(FakeChatModel):
    """첫 호출에 calculate_e74_score를 부르고, 도구 결과를 받으면 짧게 답하는 가짜 모델."""

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        last_human = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
        if self.tools_bound and not any(isinstance(m, ToolMessage) for m in messages[last_human:]):
            args = {"asOfDate": "2026-11-12"}
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content=[
                        {
                            "type": "tool_use",
                            "id": "toolu_e74",
                            "name": CALCULATE_E74_SCORE,
                            "input": args,
                            "index": 1,
                        }
                    ],
                    tool_call_chunks=[
                        {
                            "type": "tool_call_chunk",
                            "name": CALCULATE_E74_SCORE,
                            "args": json.dumps(args),
                            "id": "toolu_e74",
                            "index": 1,
                        }
                    ],
                )
            )
            yield self._final_chunk("tool_use")
            return
        yield ChatGenerationChunk(
            message=AIMessageChunk(content=[{"type": "text", "text": "305점이에요.", "index": 1}])
        )
        yield self._final_chunk("end_turn")


async def _run_e74_chat() -> list[tuple[str, dict[str, Any]]]:
    body = {"runId": "run_v", "userId": 185, "sessionId": 1, "language": "ko", "message": "점수?"}
    toolset = ChatToolset(
        FinanceTool(FixtureFinanceContext(), user_id=185, request_id="req_v", timeout=5),
        _tools(FakeE74Source()),
    )
    # 비자 규칙이 있는 v2(기본값 v1.1은 금융 도구만 쓴다).
    settings = Settings(_env_file=None, chat_prompt_version="v2")  # type: ignore[call-arg]
    events = []
    async for event in run_chat(
        ChatStreamRequest.model_validate(body),
        config=LlmConfig.from_settings(settings),
        model=E74ToolModel(delta_delay_ms=0),
        tool=toolset,
        options=ChatRunOptions(3, 30, False, FAKE_MODEL_ID),
    ):
        assert event.raw_data is not None
        events.append((str(event.event), json.loads(event.raw_data)))
    return events


def test_stream_carries_visa_tool_call_sources_and_score_card() -> None:
    events = asyncio.run(_run_e74_chat())
    by_name = {name: data for name, data in events}

    assert [name for name, _ in events] == [
        "agent.tool_call",
        "agent.delta",
        "agent.sources",
        "agent.done",
    ]
    call = by_name["agent.tool_call"]
    assert (call["toolName"], call["status"], call["resultRef"]) == (
        CALCULATE_E74_SCORE,
        "SUCCEEDED",
        {"type": "E74_ASSESSMENT", "id": 77},
    )
    assert by_name["agent.sources"]["sources"] == [
        {"type": "E74_ASSESSMENT", "id": 77, "chunkRef": None, "pageLabel": None}
    ]
    blocks = by_name["agent.done"]["blocks"]
    assert [b["type"] for b in blocks] == ["E74_SCORE_CARD"]
    assert by_name["agent.done"]["promptVersion"] == "chat-v2"


def test_record_failure_ends_only_that_tool_and_answer_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_records(_: E74AssessmentToolResponse) -> Any:
        raise ValueError("result does not fit the record models")

    # 결과 참조·근거·카드를 만들다 실패해도 채팅 실행 전체(AGENT_INTERNAL_ERROR)로 번지지 않는다.
    monkeypatch.setattr("app.chat.visa_tools.e74_records", broken_records)
    events = asyncio.run(_run_e74_chat())
    names = [name for name, _ in events]
    by_name = {name: data for name, data in events}

    assert names == ["agent.tool_call", "agent.delta", "agent.done"]
    call = by_name["agent.tool_call"]
    assert (call["toolName"], call["status"], call["errorCode"]) == (
        CALCULATE_E74_SCORE,
        "FAILED",
        "INVALID_RESULT",
    )
    # 결과 참조·근거·카드는 남지 않는다(agent.sources 없음, done.blocks null).
    assert call["resultRef"] is None
    assert by_name["agent.done"]["content"] == "305점이에요."
    assert by_name["agent.done"]["blocks"] is None


# --- 라우트: 프롬프트 버전별 도구 ----------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "names"),
    [
        ("v1.1", [TOOL_NAME]),  # 측정에 쓴 버전은 금융 도구만(측정 조건 유지)
        ("v2", [TOOL_NAME, SEARCH_VISA_POLICIES, CALCULATE_E74_SCORE]),
    ],
)
def test_route_attaches_visa_tools_only_for_v2(
    monkeypatch: pytest.MonkeyPatch, version: str, names: list[str]
) -> None:
    seen: list[list[str]] = []

    def spy(*tools: Any) -> ChatToolset:
        toolset = ChatToolset(*tools)
        seen.append([schema["name"] for schema in toolset.schemas])
        return toolset

    monkeypatch.setattr("app.api.routes.chat.ChatToolset", spy)
    token = "f" * 40
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        ai_internal_api_token=token,
        chat_fake_delta_delay_ms=0,
        chat_prompt_version=version,
    )
    app = create_app()
    app.state.rag = AppState(
        settings=settings, gateway=UnconfiguredPolicyGateway(), embedder=BgeM3Embedder()
    )
    body = {
        "runId": "run_" + "b" * 32,
        "userId": 185,
        "sessionId": 1,
        "language": "en",
        "history": [],
        "message": "hi",
    }
    response = TestClient(app).post(
        "/internal/v1/agent/chat/stream", json=body, headers={"X-Internal-Token": token}
    )
    assert response.status_code == 200
    assert seen == [names]

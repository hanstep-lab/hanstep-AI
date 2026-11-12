"""채팅 탭 '운영 경로' 검색(playground/live_search.py). HTTP는 httpx.MockTransport로 바꾼다."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from playground.e74_chat import SearchToolError, ToolContext, execute_tool
from playground.live_search import SEARCH_PATH, live_search_payload
from playground.rag_test_index import TEXT_PREVIEW_CHARS

pytestmark = pytest.mark.unit

TOKEN = "test-ai-internal-token-0123456789abcdef"
PAYLOAD_KEYS = {
    "rank",
    "score",
    "title",
    "sourceOrg",
    "page",
    "section",
    "url",
    "policyDocumentId",
    "text",
}


def _result(**overrides: Any) -> dict[str, Any]:
    """서버 PolicySearchResult(camelCase) 한 건. 합성 값."""
    result: dict[str, Any] = {
        "chunkId": "chunk-6-3",
        "policyDocumentId": 6,
        "documentVersion": "E74-SELECTION-2026-20260601",
        "chunkIndex": 3,
        "text": "고용기업 추천은 현재 근무처에서 1년 이상 근무해야 한다.",
        "title": "2026년 숙련기능인력(E-7-4) 선발 계획",
        "sourceUrl": "https://example.go.kr/notice/1",
        "sourceOrg": "법무부",
        "sourceType": "NOTICE",
        "visaTypes": ["E-7-4"],
        "score": 0.851234,
        "pageNumber": 3,
        "sectionTitle": "추천 요건",
        "effectiveFrom": None,
        "effectiveTo": None,
        "issuedOn": "2026-06-01",
    }
    result.update(overrides)
    return result


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(
        base_url="http://ai.test",
        headers={"X-Internal-Token": TOKEN},
        transport=httpx.MockTransport(handler),
    )


def test_results_become_the_test_index_payload_shape() -> None:
    seen: dict[str, Any] = {}
    long_text = "가" * (TEXT_PREVIEW_CHARS + 50)

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["token"] = request.headers["X-Internal-Token"]
        seen["body"] = json.loads(request.content)
        second = _result(policyDocumentId=3, pageNumber=None, sectionTitle=None, text=long_text)
        return httpx.Response(200, json={"results": [_result(), second]})

    payload = live_search_payload(_client(handler), "고용기업 추천 근무 기간", 5)

    assert seen == {
        "path": SEARCH_PATH,
        "token": TOKEN,
        "body": {"query": "고용기업 추천 근무 기간", "topK": 5},
    }
    assert [set(item) for item in payload] == [PAYLOAD_KEYS, PAYLOAD_KEYS]
    assert payload[0] == {
        "rank": 1,
        "score": 0.8512,
        "title": "2026년 숙련기능인력(E-7-4) 선발 계획",
        "sourceOrg": "법무부",
        "page": 3,
        "section": "추천 요건",
        "url": "https://example.go.kr/notice/1",
        "policyDocumentId": 6,
        "text": "고용기업 추천은 현재 근무처에서 1년 이상 근무해야 한다.",
    }
    assert payload[1]["rank"] == 2
    assert payload[1]["page"] is None
    assert payload[1]["text"] == long_text[:TEXT_PREVIEW_CHARS]


def test_empty_results_are_not_an_error() -> None:
    payload = live_search_payload(
        _client(lambda _: httpx.Response(200, json={"results": []})), "x", 5
    )

    assert payload == []


@pytest.mark.parametrize(
    ("status", "code", "hint"),
    [
        (503, "SEARCH_UNAVAILABLE", "승인 확인 실패"),
        (401, "UNAUTHORIZED", "AI_INTERNAL_API_TOKEN"),
        (400, "VALIDATION_ERROR", ""),
    ],
)
def test_error_status_becomes_search_tool_error(status: int, code: str, hint: str) -> None:
    client = _client(lambda _: httpx.Response(status, json={"code": code, "message": "x"}))

    with pytest.raises(SearchToolError) as info:
        live_search_payload(client, "질문", 5)

    assert f"HTTP {status} {code}" in str(info.value)
    assert hint in str(info.value)
    assert TOKEN not in str(info.value)


def test_connection_failure_and_timeout_become_search_tool_error() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(SearchToolError, match="연결하지 못함"):
        live_search_payload(_client(refuse), "질문", 5)
    with pytest.raises(SearchToolError, match="시간 초과"):
        live_search_payload(_client(slow), "질문", 5)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"results": [{"title": "필드 부족"}]}),
        httpx.Response(200, json={"items": []}),
        httpx.Response(200, text="not json"),
    ],
)
def test_malformed_response_becomes_search_tool_error(response: httpx.Response) -> None:
    with pytest.raises(SearchToolError, match="응답 형식 오류"):
        live_search_payload(_client(lambda _: response), "질문", 5)


def test_execute_tool_returns_search_failure_as_tool_error() -> None:
    def failing(query: str, top_k: int) -> list[dict[str, object]]:
        raise SearchToolError("운영 검색 실패: HTTP 503 SEARCH_UNAVAILABLE")

    ctx = ToolContext(profile=None, include_draft=True, search=failing)
    output, is_error = execute_tool("search_visa_policies", {"query": "추천"}, ctx)

    assert (output, is_error) == ("운영 검색 실패: HTTP 503 SEARCH_UNAVAILABLE", True)

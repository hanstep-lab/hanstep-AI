"""금융 정책 검색 도구(#88): 결과 형식, 미설정·잘못된 인자, 한글 없는 검색어의 dense 전환,
v4.2에서만 붙음.

실제 모델·문서 없음(가짜 검색기·가짜 순위기).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.api.routes.chat as chat_routes
from app.chat.finance_policy_tools import (
    RESULT_K,
    SEARCH_FINANCE_POLICY,
    TOOL_SCHEMA,
    FinancePolicyTool,
)
from app.chat.fx_tools import FX_RATE_SUMMARY
from app.core.config import Settings
from app.finance_policy.chunks import PolicyChunk
from app.finance_policy.search import FinancePolicySearch, PolicyHit, has_hangul
from app.integrations.policy_gateway import UnconfiguredPolicyGateway
from app.main import AppState, create_app
from app.rag.embeddings import BgeM3Embedder
from tests.unit.test_chat_stream import STREAM, TOKEN, parse_sse

pytestmark = pytest.mark.unit

HIT = PolicyHit(
    document="외국환거래규정",
    number="재정경제부고시 제2026-110호",
    ref="제4-4조",
    effective_from="2026-11-02",
    source_type="ADMIN_RULE",
    text="제4-4조(비거주자또는외국인거주자의지급등) ② … 연간미화5만불 …",
    chunk_id="unit:REG:제4-4조#1",
)
GUIDE = PolicyHit("가이드북", None, "p.152", "2022-07", "GUIDE", "대포통장 …", "unit:GUIDE:p152:3")


class FakeSearcher:
    def __init__(self, hits: list[PolicyHit]) -> None:
        self.hits = hits
        self.calls: list[tuple[str, int]] = []

    def search(self, query: str, k: int) -> tuple[list[PolicyHit], str]:
        self.calls.append((query, k))
        return self.hits[:k], "hybrid" if has_hangul(query) else "dense_only"


def _run(tool: FinancePolicyTool, args: dict[str, Any]) -> Any:
    return asyncio.run(tool.run(SEARCH_FINANCE_POLICY, args))


def test_schema_asks_for_korean_query() -> None:
    assert TOOL_SCHEMA["input_schema"]["required"] == ["queryKo"]
    assert "Write queryKo in Korean" in TOOL_SCHEMA["description"]


def test_results_have_document_ref_date_and_quote_only() -> None:
    searcher = FakeSearcher([HIT, GUIDE])
    outcome = _run(FinancePolicyTool(searcher, timeout=1), {"queryKo": "외국인거주자 송금 한도"})
    assert outcome.status == "SUCCEEDED" and outcome.sources == []  # sources는 비운다(결정)
    content = json.loads(outcome.content)
    assert content["results"][0] == {
        "document": "외국환거래규정",
        "number": "재정경제부고시 제2026-110호",
        "ref": "제4-4조",
        "effectiveFrom": "2026-11-02",
        "quote": HIT.text,
    }
    assert content["results"][1] == {
        "document": "가이드북",
        "ref": "p.152",
        "issuedOn": "2022-07",
        "quote": "대포통장 …",
    }
    assert searcher.calls == [("외국인거주자 송금 한도", RESULT_K)]
    assert outcome.measurement == {
        "query": "외국인거주자 송금 한도",
        "mode": "hybrid",
        "chunkIds": [HIT.chunk_id, GUIDE.chunk_id],
        "content": content,  # 측정 스크립트가 근거 숫자로 쓴다
    }


def test_not_configured_invalid_and_empty() -> None:
    missing = _run(FinancePolicyTool(None, timeout=1), {"queryKo": "송금"})
    assert (missing.status, missing.error_code) == ("FAILED", "SEARCH_NOT_CONFIGURED")
    bad = _run(FinancePolicyTool(FakeSearcher([]), timeout=1), {"query": "x"})
    assert (bad.status, bad.error_code) == ("REJECTED", "INVALID_ARGUMENTS")
    empty = _run(FinancePolicyTool(FakeSearcher([]), timeout=1), {"queryKo": "환율"})
    assert json.loads(empty.content) == {
        "results": [],
        "note": "No matching rule in the documents.",
    }


class FakeRetriever:
    def __init__(self, name: str) -> None:
        self.name = name
        self.queries: list[str] = []

    def search(self, query: str, k: int) -> list[tuple[int, float]]:
        self.queries.append(query)
        return [(0, 1.0)][:k]


def test_query_without_hangul_uses_dense_only() -> None:
    chunk = PolicyChunk("unit:REG:제4-4조#0", "REG", "본문", ("REG:제4-4조",), 28, "2026-11-02")
    docs = {
        "REG": {"title": "외국환거래규정", "number": "제2026-110호", "sourceType": "ADMIN_RULE"}
    }
    dense, hybrid = FakeRetriever("dense"), FakeRetriever("hybrid")
    search = FinancePolicySearch([chunk], dense, hybrid, docs)
    hits, mode = search.search("send USD 100,000 without documents", 5)
    assert mode == "dense_only" and dense.queries and not hybrid.queries
    assert hits[0].ref == "제4-4조" and hits[0].effective_from == "2026-11-02"
    _, mode = search.search("외국인거주자 송금 한도", 5)
    assert mode == "hybrid" and hybrid.queries == ["외국인거주자 송금 한도"]


_REAL_TOOLSET = chat_routes.ChatToolset


def _tool_names(version: str, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    real = _REAL_TOOLSET  # 여러 번 불러도 spy가 겹치지 않게 원래 클래스를 쓴다

    def spy(*tools: Any) -> Any:
        toolset = real(*tools)
        seen.extend(schema["name"] for schema in toolset.schemas)
        return toolset

    monkeypatch.setattr(chat_routes, "ChatToolset", spy)
    values: dict[str, Any] = {
        "ai_internal_api_token": TOKEN,
        "chat_fake_delta_delay_ms": 0,
        "chat_llm_mode": "fake",
        "chat_prompt_version": version,
    }
    settings = Settings(_env_file=None, **values)  # type: ignore[call-arg]
    app = create_app()
    app.state.rag = AppState(
        settings=settings, gateway=UnconfiguredPolicyGateway(), embedder=BgeM3Embedder()
    )
    body = {"runId": "run_" + "f" * 32, "userId": 1, "sessionId": 1, "language": "en",
            "history": [], "message": "hi"}  # fmt: skip
    response = TestClient(app).post(STREAM, json=body, headers={"X-Internal-Token": TOKEN})
    assert response.status_code == 200 and parse_sse(response.text)
    return seen


def test_tool_attached_only_for_v4_2(monkeypatch: pytest.MonkeyPatch) -> None:
    # v4.1(기본값, 기준선) 도구 구성에는 영향이 없다.
    assert SEARCH_FINANCE_POLICY not in _tool_names("v4.1", monkeypatch)
    assert SEARCH_FINANCE_POLICY in _tool_names("v4.2", monkeypatch)
    # v4.3a(스펙 가드 ①)는 v4.2와 같은 도구 구성이다.
    assert _tool_names("v4.3a", monkeypatch) == _tool_names("v4.2", monkeypatch)


def test_v4_4_adds_only_the_fx_tool_to_v4_2(monkeypatch: pytest.MonkeyPatch) -> None:
    # v4.4(#91) = v4.2 + 환율 진단 규칙: v4.2의 도구에 fx_rate_summary만 더 붙는다.
    # 다른 버전에는 붙지 않는다(도구 구성 그대로).
    v42 = _tool_names("v4.2", monkeypatch)
    assert _tool_names("v4.4", monkeypatch) == [*v42, FX_RATE_SUMMARY]
    for version in ("v1", "v2", "v3", "v4", "v4.1", "v4.2", "v4.3a"):
        assert FX_RATE_SUMMARY not in _tool_names(version, monkeypatch), version
    # v4.4d(#116) = v4.4 + 답변 표시 규칙: 도구 구성은 v4.4와 같다.
    assert _tool_names("v4.4d", monkeypatch) == _tool_names("v4.4", monkeypatch)


@pytest.mark.parametrize("enabled", [False, True])
def test_labels_setting_switches_the_tool(enabled: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    # 스펙 가드 ②: FINANCE_POLICY_LABELS 하나로 켜고 끈다(기본 off). 프롬프트는 v4.2 그대로.
    seen: list[Any] = []
    real = chat_routes.FinancePolicyTool

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("labels"))
        return real(*args, **kwargs)

    monkeypatch.setattr(chat_routes, "FinancePolicyTool", spy)
    values: dict[str, Any] = {
        "ai_internal_api_token": TOKEN,
        "chat_fake_delta_delay_ms": 0,
        "chat_llm_mode": "fake",
        "chat_prompt_version": "v4.2",
        "finance_policy_labels": enabled,
    }
    settings = Settings(_env_file=None, **values)  # type: ignore[call-arg]
    app = create_app()
    app.state.rag = AppState(
        settings=settings, gateway=UnconfiguredPolicyGateway(), embedder=BgeM3Embedder()
    )
    body = {"runId": "run_" + "e" * 32, "userId": 1, "sessionId": 1, "language": "en",
            "history": [], "message": "hi"}  # fmt: skip
    response = TestClient(app).post(STREAM, json=body, headers={"X-Internal-Token": TOKEN})
    assert response.status_code == 200
    assert len(seen) == 1
    assert (seen[0] is not None) is enabled

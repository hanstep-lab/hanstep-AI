"""테스트 채팅(플레이그라운드) 단위 테스트. 실제 Claude·임베딩 모델·네트워크 없이 실행한다."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.rag.manifest import load_manifest
from app.visa.e74.engine import assess_e74
from playground.e74_chat import (
    MAX_TOOL_ROUNDS,
    TOOLS,
    ToolContext,
    assessment_payload,
    execute_tool,
    run_turn,
)
from playground.rag_test_index import MANIFEST_PATH, active_entries, build_index, search_payload
from tests.unit._e74_builders import AS_OF, eligible_body, profile

pytestmark = pytest.mark.unit


# --- 가짜 Claude 클라이언트 --------------------------------------------------------------


@dataclass
class Block:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class Usage:
    input_tokens: int = 10
    output_tokens: int = 5


@dataclass
class Response:
    content: list[Block]
    stop_reason: str
    usage: Usage = field(default_factory=Usage)


class FakeMessages:
    def __init__(self, responses: list[Response]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Response:
        # 호출 시점의 messages를 복사해 둔다(이후 history가 늘어나도 그대로 남게).
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


class FakeClient:
    def __init__(self, responses: list[Response]) -> None:
        self.messages = FakeMessages(responses)


def _ctx(**overrides: Any) -> ToolContext:
    base: dict[str, Any] = {
        "profile": profile(eligible_body()),
        "include_draft": True,
        "search": lambda query, top_k: [{"rank": 1, "title": "선발 계획", "text": query}][:top_k],
        "today": AS_OF,
    }
    return ToolContext(**(base | overrides))


def test_tool_definitions_are_closed_schemas() -> None:
    assert [t["name"] for t in TOOLS] == ["search_visa_policies", "calculate_e74_score"]
    for tool in TOOLS:
        assert tool["input_schema"]["additionalProperties"] is False


def test_turn_runs_tools_and_returns_final_text() -> None:
    client = FakeClient(
        [
            Response(
                [
                    Block("text", text="확인할게요."),
                    Block("tool_use", id="t1", name="calculate_e74_score", input={}),
                    Block(
                        "tool_use", id="t2", name="search_visa_policies", input={"query": "가점"}
                    ),
                ],
                "tool_use",
            ),
            Response([Block("text", text="총점은 305점입니다.")], "end_turn"),
        ]
    )
    history: list[dict[str, Any]] = []
    turn = run_turn(client, "claude-haiku-4-5", history, "제 점수는?", _ctx())

    assert turn.text == "총점은 305점입니다." and turn.stop_reason == "end_turn"
    assert [c.name for c in turn.tool_calls] == ["calculate_e74_score", "search_visa_policies"]
    assert json.loads(turn.tool_calls[0].output)["score"]["total"] == 305
    assert (turn.input_tokens, turn.output_tokens) == (20, 10)
    # 두 도구 결과는 한 사용자 메시지에 담긴다.
    second = client.messages.requests[1]["messages"]
    results = second[-1]["content"]
    assert second[-1]["role"] == "user" and [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert client.messages.requests[0]["model"] == "claude-haiku-4-5"
    assert len(history) == 4  # user, assistant(tool_use), user(tool_result), assistant


def test_history_carries_into_next_turn() -> None:
    client = FakeClient(
        [
            Response([Block("text", text="a")], "end_turn"),
            Response([Block("text", text="b")], "end_turn"),
        ]
    )
    history: list[dict[str, Any]] = []
    run_turn(client, "m", history, "첫 질문", _ctx())
    run_turn(client, "m", history, "두 번째", _ctx())
    assert [m["role"] for m in client.messages.requests[1]["messages"]] == [
        "user",
        "assistant",
        "user",
    ]


@pytest.mark.parametrize(
    ("stop_reason", "notice"),
    [("max_tokens", "max_tokens"), ("refusal", "refusal"), ("end_turn", None)],
)
def test_stop_reasons_are_reported(stop_reason: str, notice: str | None) -> None:
    client = FakeClient([Response([Block("text", text="...")], stop_reason)])
    turn = run_turn(client, "m", [], "질문", _ctx())
    assert turn.stop_reason == stop_reason
    assert (notice in turn.notice) if notice else turn.notice is None  # type: ignore[operator]


def test_tool_rounds_are_capped() -> None:
    loop = Response([Block("tool_use", id="t", name="calculate_e74_score", input={})], "tool_use")
    client = FakeClient([loop] * MAX_TOOL_ROUNDS)
    turn = run_turn(client, "m", [], "질문", _ctx())
    assert turn.stop_reason == "max_rounds" and len(turn.tool_calls) == MAX_TOOL_ROUNDS


def test_tool_errors_are_returned_not_raised() -> None:
    no_index, err = execute_tool("search_visa_policies", {"query": "x"}, _ctx(search=None))
    assert err and "검색 인덱스" in no_index
    bad_profile, err = execute_tool("calculate_e74_score", {}, _ctx(profile=None))
    assert err and "프로필" in bad_profile
    bad_date, err = execute_tool("calculate_e74_score", {"as_of_date": "26-10-25"}, _ctx())
    assert err and "형식" in bad_date
    unknown, err = execute_tool("delete_everything", {}, _ctx())
    assert err and "알 수 없는 도구" in unknown


def test_calculation_uses_the_engine_result_unchanged() -> None:
    output, err = execute_tool("calculate_e74_score", {"as_of_date": "2026-10-25"}, _ctx())
    assert not err
    expected = assessment_payload(assess_e74(profile(eligible_body()), AS_OF, include_draft=True))
    assert json.loads(output) == json.loads(json.dumps(expected, ensure_ascii=False))
    assert json.loads(output)["status"] == "ELIGIBLE"


def test_search_top_k_is_clamped() -> None:
    seen: list[int] = []

    def search(query: str, top_k: int) -> list[dict[str, object]]:
        seen.append(top_k)
        return []

    execute_tool("search_visa_policies", {"query": "x", "top_k": 99}, _ctx(search=search))
    execute_tool("search_visa_policies", {"query": "x", "top_k": 3}, _ctx(search=search))
    assert seen == [5, 3]


# --- 테스트 인덱스 ------------------------------------------------------------------------


class FakeEmbedder:
    """텍스트 해시로 만든 단위 벡터. 같은 텍스트 → 같은 벡터."""

    model_name = "fake"

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        raw = [b / 255 + 0.01 for b in digest[:16]]
        norm = sum(v * v for v in raw) ** 0.5
        return [v / norm for v in raw]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


def test_superseded_original_is_left_out() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    paths = [e.path for e in active_entries(manifest.sources, "visa")]
    assert "visa/selection/e74_selection_20260601.pdf" in paths
    assert "visa/selection/e74_selection_2026_original.hwp" not in paths
    assert all(p.startswith("visa/") for p in paths)


def test_index_builds_caches_and_searches(tmp_path: Path) -> None:
    embedder = FakeEmbedder()
    first = build_index(embedder, cache_dir=tmp_path, domain="visa")
    assert first.chunks and not first.cache_hit
    assert first.vectors.shape == (len(first.chunks), 16)
    second = build_index(embedder, cache_dir=tmp_path, domain="visa")
    assert second.cache_hit and len(second.chunks) == len(first.chunks)

    target = first.chunks[3]
    results = search_payload(first, embedder, target.text, top_k=2)
    assert results[0]["text"] == target.text[:800] and results[0]["rank"] == 1
    assert {"title", "sourceOrg", "page", "url", "policyDocumentId"} <= set(results[0])


def test_payload_is_compact() -> None:
    payload = assessment_payload(
        assess_e74(profile(eligible_body()), date(2026, 10, 25), include_draft=True)
    )
    assert "items" not in payload
    assert all(r["status"] != "NOT_APPLICABLE" for r in payload["requirements"])

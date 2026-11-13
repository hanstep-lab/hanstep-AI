"""테스트 채팅(플레이그라운드) 단위 테스트. 실제 Claude·임베딩 모델·네트워크 없이 실행한다."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.rag.manifest import load_manifest
from app.visa.e74.engine import assess_e74
from playground.e74_chat import (
    CHAT_THINKING,
    MAX_TOOL_ROUNDS,
    TOOLS,
    ToolContext,
    assessment_payload,
    execute_tool,
    run_turn,
    strip_emoji,
)
from playground.rag_test_index import MANIFEST_PATH, active_entries, build_index, search_payload
from tests.unit._e74_builders import AS_OF, eligible_body, profile

pytestmark = pytest.mark.unit


# --- 가짜 Claude 클라이언트 --------------------------------------------------------------


@dataclass
class Block:
    """가짜 응답 콘텐츠 블록(text 또는 tool_use)."""

    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class Usage:
    """가짜 토큰 사용량."""

    input_tokens: int = 10
    output_tokens: int = 5


@dataclass
class Response:
    """가짜 messages.create 응답."""

    content: list[Block]
    stop_reason: str
    usage: Usage = field(default_factory=Usage)


class FakeMessages:
    """준비한 응답을 차례로 돌려주고 받은 요청을 기록하는 가짜 messages API."""

    def __init__(self, responses: list[Response | Exception]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Response:
        # 호출 시점의 messages를 복사해 둔다(이후 history가 늘어나도 그대로 남게).
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        reply = self.responses.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeClient:
    """가짜 Claude 클라이언트."""

    def __init__(self, responses: list[Response | Exception]) -> None:
        self.messages = FakeMessages(responses)


class ApiError(Exception):
    """anthropic.APIStatusError처럼 status_code를 가진 오류."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


THINKING_REJECTED = ApiError(400, '"thinking.type.adaptive" is not supported for this model')


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
    turn = run_turn(client, "claude-haiku-5-5", history, "제 점수는?", _ctx())

    assert turn.text == "총점은 305점입니다." and turn.stop_reason == "end_turn"
    assert [c.name for c in turn.tool_calls] == ["calculate_e74_score", "search_visa_policies"]
    assert json.loads(turn.tool_calls[0].output)["score"]["total"] == 305
    assert (turn.input_tokens, turn.output_tokens) == (20, 10)
    # 두 도구 결과는 한 사용자 메시지에 담긴다.
    second = client.messages.requests[1]["messages"]
    results = second[-1]["content"]
    assert second[-1]["role"] == "user" and [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert client.messages.requests[0]["model"] == "claude-haiku-5-5"
    assert len(history) == 4  # 사용자 → 어시스턴트(tool_use) → 사용자(tool_result) → 어시스턴트


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


def _broken_calculate(as_of: date) -> dict[str, Any]:
    raise RuntimeError("unexpected")


@pytest.mark.parametrize(
    ("after_tool_use", "ctx_overrides", "error"),
    [
        ([ApiError(529, "overloaded")], {}, ApiError),  # 도구 결과를 보낸 호출이 실패
        ([], {"calculate": _broken_calculate}, RuntimeError),  # 도구 실행 중 예외
    ],
    ids=["api_error", "tool_raises"],
)
def test_failed_turn_after_tool_use_is_removed_from_history(
    after_tool_use: list[Exception], ctx_overrides: dict[str, Any], error: type[Exception]
) -> None:
    # tool_result만 빼면 짝 없는 tool_use가 남아 이후 요청이 모두 400이 된다.
    client = FakeClient(
        [
            Response([Block("text", text="a")], "end_turn"),
            Response(
                [Block("tool_use", id="t1", name="calculate_e74_score", input={})], "tool_use"
            ),
            *after_tool_use,
            Response([Block("text", text="c")], "end_turn"),
        ]
    )
    history: list[dict[str, Any]] = []
    run_turn(client, "m", history, "첫 질문", _ctx())
    before = list(history)
    with pytest.raises(error):
        run_turn(client, "m", history, "제 점수는?", _ctx(**ctx_overrides))
    assert history == before

    run_turn(client, "m", history, "다시 질문", _ctx())
    messages = client.messages.requests[-1]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[-1]["content"] == "다시 질문"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("총 305점 ✅\n✅ 한국 체류 기간", "총 305점\n한국 체류 기간"),
        ("신청할 수 있어요! 🎉 점수는 305점이에요", "신청할 수 있어요! 점수는 305점이에요"),
        ("1️⃣ 한국어 올리기", "1. 한국어 올리기"),
        ("⚠️ 서류를 확인해 주세요", "서류를 확인해 주세요"),
        ("👨‍👩‍👧 가족 초청", "가족 초청"),  # ZWJ로 이은 묶음
        ("🇰🇷 한국", "한국"),  # 국기
        ("서류\n   - TOPIK 성적표 📄", "서류\n   - TOPIK 성적표"),  # 목록 들여쓰기는 남긴다
        # 이모지가 아닌 기호와 숫자는 그대로 둔다.
        (
            "기본 205점 + 가점 100점 · 출처: 선발 계획 p.3 → 다음 단계 (+40점, ©)",
            "기본 205점 + 가점 100점 · 출처: 선발 계획 p.3 → 다음 단계 (+40점, ©)",
        ),
    ],
)
def test_strip_emoji(raw: str, expected: str) -> None:
    assert strip_emoji(raw) == expected


def test_emoji_is_removed_from_answer_and_history() -> None:
    client = FakeClient(
        [
            Response([Block("text", text="305점이에요 🎉")], "end_turn"),
            Response([Block("text", text="b")], "end_turn"),
        ]
    )
    history: list[dict[str, Any]] = []
    turn = run_turn(client, "m", history, "첫 질문", _ctx())
    run_turn(client, "m", history, "두 번째", _ctx())

    assert turn.text == "305점이에요"
    # 다음 턴에 보내는 기록에도 이모지가 없어야 모델이 따라 쓰지 않는다.
    assert client.messages.requests[1]["messages"][1]["content"] == [
        {"type": "text", "text": "305점이에요"}
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


# --- thinking ------------------------------------------------------------------------------


def test_every_round_asks_for_adaptive_thinking() -> None:
    client = FakeClient(
        [
            Response(
                [
                    Block("thinking"),
                    Block("tool_use", id="t1", name="calculate_e74_score", input={}),
                ],
                "tool_use",
            ),
            Response([Block("text", text="총점은 305점입니다.")], "end_turn"),
        ]
    )
    history: list[dict[str, Any]] = []
    turn = run_turn(client, "claude-haiku-5-5", history, "제 점수는?", _ctx())
    assert CHAT_THINKING == {"type": "adaptive"}
    assert [r["thinking"] for r in client.messages.requests] == [CHAT_THINKING] * 2
    assert turn.thinking_used is True
    # thinking 블록은 다음 요청에 그대로 넘긴다(도구 사용 중 추론 유지).
    assert history[1]["content"][0].type == "thinking"


def test_turn_without_thinking_blocks_reports_it() -> None:
    client = FakeClient([Response([Block("text", text="안녕하세요.")], "end_turn")])
    assert run_turn(client, "m", [], "안녕", _ctx()).thinking_used is False


def test_rejected_thinking_is_retried_once_without_it(caplog: pytest.LogCaptureFixture) -> None:
    client = FakeClient([THINKING_REJECTED, Response([Block("text", text="네.")], "end_turn")])
    with caplog.at_level(logging.WARNING, logger="playground.e74_chat"):
        turn = run_turn(client, "claude-haiku-4-5", [], "안녕", _ctx())
    assert turn.text == "네."
    first, second = client.messages.requests
    assert first["thinking"] == CHAT_THINKING and "thinking" not in second
    assert "rejected thinking=adaptive (400) for model=claude-haiku-4-5" in caplog.text


def test_failed_retry_and_other_errors_are_raised() -> None:
    retry_fails = FakeClient([THINKING_REJECTED, ApiError(500, "overloaded")])
    with pytest.raises(ApiError):
        run_turn(retry_fails, "m", [], "안녕", _ctx())
    assert len(retry_fails.messages.requests) == 2

    not_thinking = FakeClient([ApiError(400, "invalid tool schema")])
    with pytest.raises(ApiError):
        run_turn(not_thinking, "m", [], "안녕", _ctx())
    assert len(not_thinking.messages.requests) == 1

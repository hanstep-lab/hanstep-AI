"""금융 정책 답변 위험 문장 검사기(#93): 규칙 v3 모듈과 run_chat 적용(문장 단위 버퍼·대체·로그).

설계: specs/rag/finance_policy_answer_guard_v3.md 6절.
실제 Anthropic 호출 없음(가짜 모델·가짜 도구).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

import anthropic
import httpx
import pytest
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.messages import BaseMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk

from app.chat.fake_llm import FAKE_MODEL_ID, FakeChatModel, _tool_call_chunk
from app.chat.finance_policy_tools import SEARCH_FINANCE_POLICY
from app.chat.schemas import ChatStreamRequest, LlmConfig
from app.chat.stream import ChatRunOptions, run_chat
from app.chat.tools import ToolOutcome
from app.core.config import Settings
from app.finance_policy.answer_guard import (
    REPLACEMENT,
    SentenceGuard,
    classify_v3,
    d3_branch,
    split_complete,
)

pytestmark = pytest.mark.unit

# 확정 위험 8문장(설계 문서 17-3절 7건 + 머지 재측정 A03#1).
# 출처: reviews/finance_policy_answer_guard_v3_result.md.
CONFIRMED = [
    "For you, the rule that applies is Article 4-4 (effective 2026-11-02), which lets a foreign "
    "resident send up to USD 5,000 per transaction without supporting documents.",
    "Under Foreign Exchange Transaction Regulation, Article 4-4, paragraph 4 (effective "
    "2026-11-02), non-residents and foreign residents can send up to 5,000 dollars per "
    "transaction without supporting documents.",
    "For foreign residents, Article 4-4(4) (effective 2026-11-02) allows sending without "
    "supporting documents only for a single payment of up to 5,000 dollars.",
    "Under Article 4-4(4), a single transaction of up to US$5,000 can be sent without supporting "
    "documents.",
    "Under Foreign Exchange Transaction Regulation, Article 4-4 (effective 2026-11-02), both "
    "foreign residents and non-residents can send up to US$5,000 per transaction without "
    "supporting documents, and wages earned in Korea can be sent through a designated foreign "
    "exchange bank.",
    "Under Article 4-4(4) (effective 2026-11-02), transfers of up to USD 5,000 per transaction do "
    "not need supporting documents.",
    "Under the Foreign Exchange Transaction Regulation, Article 4-4(4) (effective 2026-11-02), a "
    "foreigner resident in Korea can send up to USD 5,000 per transaction without submitting "
    "supporting documents.",
    "Instead, Article 4-4 (effective 2026-11-02) applies, which lets you send income from "
    "domestic work through a designated foreign exchange bank, and allows a single transfer of "
    "up to USD 5,000 without supporting documents.",
]
# 걸리면 안 되는 문장: v2 오탐(after A10#3), 제2-31조 한도 안내, 수령 안내.
MUST_PASS = [
    "Amounts above USD 5,000 in one transfer would need documents, so split transfers may not be "
    "the way around that.",
    "Under the regulation, Article 2-31 (effective 2026-11-02), a fintech small-sum remittance "
    "company can send up to USD 5,000 per transaction and up to USD 50,000 per person per year.",
    "Under the regulation, you do not need to submit supporting documents to receive a transfer "
    "of up to USD 5,000 per transaction.",
]
INTRO = "Under the regulation, foreign residents use a designated bank. "


def _guarded(text: str, mode: str = "replace") -> tuple[str, SentenceGuard]:
    guard = SentenceGuard(mode)  # type: ignore[arg-type]
    out = "".join(guard.feed(w) for w in re.findall(r"\S+\s*", text)) + guard.finish()
    return out, guard


# --- 규칙 모듈 --------------------------------------------------------------------------------


@pytest.mark.parametrize("sentence", CONFIRMED)
def test_confirmed_risky_sentences_are_replaced(sentence: str) -> None:
    assert classify_v3(sentence) == "R3"
    out, guard = _guarded(INTRO + sentence + " Please check with your bank.")
    assert out == INTRO + REPLACEMENT + " Please check with your bank."
    assert len(guard.hits) == 1 and guard.replaced


@pytest.mark.parametrize("sentence", MUST_PASS)
def test_safe_sentences_pass_unchanged(sentence: str) -> None:
    out, guard = _guarded(INTRO + sentence)
    assert out == INTRO + sentence
    assert guard.hits == []


def test_d3_branches() -> None:
    assert d3_branch("send USD 5,000 without supporting documents") == "a"
    assert d3_branch("for USD 5,000, documents are not required") == "b"
    assert d3_branch("5천불까지 증빙 없이 송금") == "c"


def test_split_keeps_abbreviations_and_decimals() -> None:
    text = "Banks in the U.S. ask, e.g. for ID. Pay 5,000.00 now. Art. 4 i.e. this. Next"
    pieces, rest = split_complete(text)
    assert pieces == [
        "Banks in the U.S. ask, e.g. for ID. ",
        "Pay 5,000.00 now. ",
        "Art. 4 i.e. this. ",
    ]
    assert rest == "Next"
    # 버퍼 끝의 마침표는 다음 글자를 볼 때까지 남는다.
    assert split_complete("Done.") == ([], "Done.")
    assert split_complete("One\nTwo") == (["One\n"], "Two")


def test_several_hits_replace_once_and_drop_the_rest() -> None:
    text = INTRO + CONFIRMED[0] + " " + CONFIRMED[3] + " Thanks."
    out, guard = _guarded(text)
    assert out == INTRO + REPLACEMENT + " Thanks."
    assert out.count(REPLACEMENT) == 1
    assert len(guard.hits) == 2


def test_log_mode_returns_text_unchanged_and_counts() -> None:
    text = INTRO + CONFIRMED[0]
    out, guard = _guarded(text, "log")
    assert out == text
    assert len(guard.hits) == 1 and not guard.replaced
    assert guard.metrics()["sentences"] == [CONFIRMED[0]]


# --- run_chat 적용 -----------------------------------------------------------------------------


class PolicyModel(FakeChatModel):
    """결과가 없으면 search_finance_policy를 부르고, 결과가 오면 answer를 단어로 흘린다."""

    answer: str = ""
    fail_after_answer: bool = False

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        self.probe["calls"] = self.probe.get("calls", 0) + 1
        if self.tools_bound and not any(isinstance(m, ToolMessage) for m in messages):
            yield ChatGenerationChunk(
                message=_tool_call_chunk(SEARCH_FINANCE_POLICY, {"queryKo": "송금"}, "toolu_p")
            )
            yield self._final_chunk("tool_use")
            return
        async for piece in self._stream_text(self.answer):
            yield piece
        if self.fail_after_answer:
            request = httpx.Request("POST", "https://fake.invalid/v1/messages")
            raise anthropic.APIConnectionError(request=request)
        yield self._final_chunk("end_turn")


class PolicyTool:
    def __init__(self, status: str = "SUCCEEDED") -> None:
        self.status = status

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": SEARCH_FINANCE_POLICY,
                "description": "test",
                "input_schema": {"type": "object", "properties": {}},
            }
        ]

    async def run(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        return ToolOutcome(name, arguments, self.status, '{"results": []}', 1)


def _request() -> ChatStreamRequest:
    return ChatStreamRequest.model_validate(
        {
            "runId": "run_" + "b" * 32,
            "userId": 1,
            "sessionId": 9,
            "message": "Can I send 5,000 dollars without papers?",
            "language": "en",
            "history": [],
        }
    )


async def _run(
    answer: str,
    *,
    mode: str = "replace",
    status: str = "SUCCEEDED",
    fail: bool = False,
) -> list[tuple[str, dict[str, Any]]]:
    settings = Settings(_env_file=None, chat_prompt_version="v4.2")  # type: ignore[call-arg]
    model = PolicyModel(delta_delay_ms=0, answer=answer, fail_after_answer=fail)
    events = []
    async for event in run_chat(
        _request(),
        config=LlmConfig.from_settings(settings),
        model=model,
        tool=PolicyTool(status),
        options=ChatRunOptions(3, 30, True, FAKE_MODEL_ID, mode),  # type: ignore[arg-type]
    ):
        assert event.raw_data is not None
        events.append((str(event.event), json.loads(event.raw_data)))
    return events


def _deltas(events: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [d["text"] for n, d in events if n == "agent.delta"]


def _metrics(events: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    return next(d for n, d in events if n == "agent.metrics")


RISKY_ANSWER = INTRO + CONFIRMED[1] + " " + CONFIRMED[6] + " Please confirm with your bank."


async def test_replace_mode_streams_checked_sentences_and_done_matches() -> None:
    events = await _run(RISKY_ANSWER)
    deltas = _deltas(events)
    done = events[-1][1]
    assert events[-1][0] == "agent.done"
    assert done["content"] == "".join(deltas)
    assert done["content"] == INTRO + REPLACEMENT + " Please confirm with your bank."
    assert "without supporting documents" not in done["content"]
    # 문장 단위로 나간다(단어마다가 아님).
    assert deltas == [INTRO, REPLACEMENT + " ", "Please confirm with your bank."]
    guard = _metrics(events)["answerGuard"]
    assert guard["mode"] == "replace" and guard["buffered"] is True
    assert guard["hits"] == 2 and guard["branches"] == ["a", "a"] and guard["replaced"] is True
    assert guard["sentences"] == [CONFIRMED[1], CONFIRMED[6]]


async def test_log_mode_streams_words_unchanged() -> None:
    events = await _run(RISKY_ANSWER, mode="log")
    deltas = _deltas(events)
    assert "".join(deltas) == RISKY_ANSWER == events[-1][1]["content"]
    assert len(deltas) == len(re.findall(r"\S+\s*", RISKY_ANSWER))
    guard = _metrics(events)["answerGuard"]
    assert guard["hits"] == 2 and guard["replaced"] is False and guard["buffered"] is False


async def test_off_mode_has_no_guard() -> None:
    events = await _run(RISKY_ANSWER, mode="off")
    assert "".join(_deltas(events)) == RISKY_ANSWER
    assert "answerGuard" not in _metrics(events)


async def test_failed_policy_search_is_not_buffered() -> None:
    events = await _run(RISKY_ANSWER, status="FAILED")
    deltas = _deltas(events)
    assert "".join(deltas) == RISKY_ANSWER
    assert len(deltas) == len(re.findall(r"\S+\s*", RISKY_ANSWER))
    assert "answerGuard" not in _metrics(events)


async def test_turn_without_policy_tool_streams_immediately() -> None:
    # 기본 가짜 모델은 get_financial_analysis만 부른다(검사기 없음).
    from tests.unit.test_chat_stream import _run as run_finance

    events = await run_finance(metrics=True)
    assert "answerGuard" not in _metrics(events)
    assert len(_deltas(events)) > 3


async def test_error_drops_unchecked_tail() -> None:
    events = await _run(INTRO + "You can send USD 5,000 without supporting", fail=True)
    assert events[-1][0] == "agent.error"
    # 완성된 첫 문장만 나가고, 검사하지 않은 마지막 조각은 나가지 않는다.
    assert "".join(_deltas(events)) == INTRO


async def test_guard_log_line_has_no_answer_text(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="app.chat.stream"):
        await _run(RISKY_ANSWER)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("chat guard")]
    assert len(lines) == 1
    assert "rule=v3 hits=2 branches=a,a action=replace" in lines[0]
    assert "5,000" not in lines[0] and "Article" not in lines[0] and "user" not in lines[0]


def test_setting_default_is_replace() -> None:
    assert Settings(_env_file=None).finance_policy_answer_guard == "replace"  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        Settings(_env_file=None, finance_policy_answer_guard="on")  # type: ignore[call-arg]

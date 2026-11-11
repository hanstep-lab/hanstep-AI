from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.chat.number_check import check_answer
from app.chat.tools import summarize

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "chat" / "finance_context_minh.json"
TOOL_RESULT: dict[str, Any] = summarize(json.loads(FIXTURE.read_text(encoding="utf-8")))


def _check(answer: str, texts: list[str] | None = None) -> Any:
    return check_answer(answer, [TOOL_RESULT], texts or [])


@pytest.mark.parametrize(
    "answer",
    [
        "Your spare money is 675,317 a month.",
        "Your spare money is 675317.",
        "매달 약 68만 원이 남습니다.",
        "You have KRW 675,317 left.",
        "About 675k won is left.",
        "Roughly 0.68 million KRW.",
        "Over the last 12 full months your income was KRW 2,350,000.",
        "The analysis covers 2025-11-01 to 2026-10-31.",
        "Your goal is ₩6,000,000 by 2027년 9월 30일.",
    ],
)
def test_supported_numbers(answer: str) -> None:
    result = _check(answer)
    assert result.found, answer
    assert result.unsupported == [], result.to_json()
    assert result.ambiguous == []


@pytest.mark.parametrize(
    ("answer", "flagged"),
    [
        ("In a year you will save 8,103,804 won.", "8,103,804 won"),
        ("You send 29.8% of your income home.", "29.8%"),
        ("You have about 80만 원 left.", "80만 원"),
    ],
)
def test_unsupported_numbers(answer: str, flagged: str) -> None:
    assert _check(answer).unsupported == [flagged]


def test_numbers_from_question_and_history_are_allowed() -> None:
    result = _check("Yes, 1,000,000 won is not right.", ["My spare money is 1,000,000 won, right?"])
    assert result.unsupported == []


def test_dates_and_ordinals_not_in_context_are_ambiguous() -> None:
    result = _check("5. Pay rent on the 25th.\n6. Check again in 2024-03.")
    assert result.unsupported == []
    assert result.ambiguous == ["5.", "25th", "6.", "2024-03"]
    assert [f.kind for f in result.found] == ["ordinal", "ordinal", "ordinal", "date"]

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
        "The analysis ends on 2026-10-31.",
        "The analysis ends on October 31.",
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
        # date parts (2025, 2026, 10, 31 from the period) never back amounts or percents
        ("You send 31% of your income home.", "31%"),
        ("You have KRW 31 left.", "KRW 31"),
        ("You have KRW 2,000 left.", "KRW 2,000"),
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


@pytest.mark.parametrize(
    "answer",
    [
        "I can also help with your E-7-4 visa score.",
        "E-7-4 and F-2-7 are different visas.",
        "Your E-7-4 (skilled worker) pre-check is ready.",
    ],
)
def test_visa_codes_are_not_numbers(answer: str) -> None:
    # 2026-11-16 v3 회귀 O04: "E-7-4"가 숫자 7과 4로 잡혀 근거 없는 숫자가 됐다.
    result = _check(answer)
    assert result.found == []


def test_other_numbers_next_to_a_visa_code_are_still_checked() -> None:
    result = _check("E-7-4 needs 300 points; your spare money is 675,317 a month.")
    assert [f.text for f in result.found] == ["300", "675,317"]
    assert result.unsupported == ["300"]


@pytest.mark.parametrize(
    ("answer", "flagged"),
    [
        # 대문자-숫자-숫자 형식만 비자 코드다.
        ("You need e-7-4 points of 7.", ["7", "4", "7"]),
        ("Plan A-7 costs 37.", ["7", "37"]),
        ("Code XE-7-4 is used.", ["7", "4"]),
    ],
)
def test_only_the_visa_code_format_is_skipped(answer: str, flagged: list[str]) -> None:
    assert _check(answer).unsupported == flagged

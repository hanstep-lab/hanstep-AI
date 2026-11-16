"""송금 재질의 평가 세트(specs/chat/remit_questions_v1.json) 형식 검증. 문항은 고치지 않는다."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

SPEC = Path(__file__).resolve().parents[2] / "specs" / "chat" / "remit_questions_v1.json"
FIELDS = {
    "id",
    "text",
    "category",
    "expectedAction",
    "expectedRecipient",
    "expectedCurrency",
    "expectedAmount",
    "followUpReply",
    "note",
}
ACTIONS = {
    "clear": {"PROCEED_TO_CONFIRM"},
    "ambiguous": {"ASK_RECIPIENT", "ASK_CURRENCY", "ASK_AMOUNT", "ASK_MORE_THAN_ONE"},
    "out_of_scope": {"DECLINE_OR_GUIDE"},
}
# 되물어야 하는 항목은 기대값이 비어 있다.
ASKED_FIELD = {
    "ASK_RECIPIENT": "expectedRecipient",
    "ASK_CURRENCY": "expectedCurrency",
    "ASK_AMOUNT": "expectedAmount",
}
SLOTS = ("expectedRecipient", "expectedCurrency", "expectedAmount")


def _questions() -> list[dict[str, Any]]:
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    assert set(spec) == {"version", "questions"}
    assert spec["version"] == "v1"
    questions: list[dict[str, Any]] = spec["questions"]
    return questions


def test_thirty_questions_with_unique_ids() -> None:
    questions = _questions()
    assert len(questions) == 30
    assert [q["id"] for q in questions] == [f"S{i:02d}" for i in range(1, 31)]


@pytest.mark.parametrize("question", _questions(), ids=lambda q: q["id"])
def test_question_fields(question: dict[str, Any]) -> None:
    assert set(question) == FIELDS
    assert question["text"].strip() and question["note"].strip()
    assert question["expectedAction"] in ACTIONS[question["category"]]
    assert question["expectedRecipient"] in ("MOTHER", "SELF", None)
    assert question["expectedCurrency"] in ("KRW", "PHP", None)
    amount = question["expectedAmount"]
    assert amount is None or (isinstance(amount, int) and amount > 0)

    action = question["expectedAction"]
    if question["category"] == "clear":
        assert all(question[slot] is not None for slot in SLOTS)
    if question["category"] == "ambiguous":
        # 애매한 문항만 2턴째 답이 있다.
        assert question["followUpReply"] and question["followUpReply"].strip()
    else:
        assert question["followUpReply"] is None
    if action in ASKED_FIELD:
        assert question[ASKED_FIELD[action]] is None
    if action == "ASK_MORE_THAN_ONE":
        assert sum(question[slot] is None for slot in SLOTS) >= 2


def test_every_action_is_covered() -> None:
    actions = [q["expectedAction"] for q in _questions()]
    assert {a: actions.count(a) for a in dict.fromkeys(actions)} == {
        "PROCEED_TO_CONFIRM": 9,
        "ASK_RECIPIENT": 4,
        "ASK_CURRENCY": 4,
        "ASK_AMOUNT": 3,
        "ASK_MORE_THAN_ONE": 4,
        "DECLINE_OR_GUIDE": 6,
    }

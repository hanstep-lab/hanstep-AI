"""필리핀 국적 현지화 채점(scripts/_ph_check.py, #130) 단위 테스트."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from app.chat.number_check import check_answer
from app.finance_policy.answer_guard import classify_v3

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
LC = importlib.import_module("_lang_check")
PH = importlib.import_module("_ph_check")

pytestmark = pytest.mark.unit


def test_counts_allowed_expressions_once_each() -> None:
    answer = "Kumusta po! Your plan is ready. Maraming salamat po! Ingat po!"
    check = PH.ph_check(answer, first_turn=True)
    assert check["expressions"] == ["Kumusta po!", "Maraming salamat po!", "Ingat po!"]
    # "Maraming salamat po" 안의 "salamat po"를 따로 세지 않는다.
    assert check["count"] == 3 and check["overLimit"] is True
    assert check["greeting"] is True and check["greetingLater"] is False
    assert check["otherTagalog"] == []


def test_greeting_after_the_first_turn_is_flagged() -> None:
    check = PH.ph_check("kumusta po. Here is your balance.", first_turn=False)
    assert (check["count"], check["greetingLater"]) == (1, True)
    assert (
        PH.ph_check("Salamat po! Here is your balance.", first_turn=False)["greetingLater"] is False
    )


def test_other_tagalog_words_outside_the_allowed_expressions() -> None:
    check = PH.ph_check("Salamat po! Your nanay will get the padala tomorrow, po.", first_turn=True)
    assert check["otherTagalog"] == ["nanay", "padala", "po"]
    # 영어와 철자가 같은 단어(ate 등)와 허용 표현 안의 단어는 세지 않는다.
    assert PH.ph_check("I ate lunch. Ingat po!", first_turn=True)["otherTagalog"] == []


def test_expression_in_a_sentence_with_a_number() -> None:
    bad = PH.ph_check("Salamat po, she will receive PHP 5,300.", first_turn=True)
    assert bad["numericSentences"] == ["Salamat po, she will receive PHP 5,300."]
    # 숫자 문장과 표현이 다른 문장이면 괜찮다.
    good = PH.ph_check("Salamat po! She will receive PHP 5,300.", first_turn=True)
    assert good["numericSentences"] == []


def test_forbidden_phrases_and_assumed_family() -> None:
    check = PH.ph_check(
        "Your love and sacrifice for your family is a duty. God bless! Ask your wife too.",
        first_turn=True,
    )
    assert check["forbidden"] == ["bless", "duty", "god", "love", "sacrifice"]
    assert check["assumedFamily"] == ["your wife"]
    clean = PH.ph_check("Here is a plan that covers your own savings first.", first_turn=True)
    assert (clean["forbidden"], clean["assumedFamily"], clean["count"]) == ([], [], 0)


def test_summary_by_role_marks_roles_that_must_have_no_expression() -> None:
    rows = [
        {
            "variant": "v4.8",
            "phRole": "complaint",
            "answer": "Salamat po! Sorry.",
            "historyTurns": 0,
        },
        {"variant": "v4.8", "phRole": "complaint", "answer": "I am sorry.", "historyTurns": 0},
        {
            "variant": "v4.8",
            "phRole": "multi_turn",
            "answer": "Kumusta po! Sure.",
            "historyTurns": 2,
        },
    ]
    summary = {s["role"]: s for s in PH.summarize(rows)}
    assert summary["complaint"]["withExpression"] == 1
    assert summary["complaint"]["mustBeZero"] is True
    assert summary["multi_turn"]["greetingLater"] == 1
    assert summary["multi_turn"]["mustBeZero"] is False
    assert "(0이어야 함)" in PH.render(PH.summarize(rows))


def test_allowed_expressions_pass_the_existing_answer_checks() -> None:
    # #130: 허용 표현은 로마자라 영어 답변 언어 검사(한글 혼입)·숫자 검사·답변 검사기에
    # 걸리지 않는다.
    answer = (
        "Kumusta po! You have KRW 520,000 left each month based on past averages. "
        "Maraming salamat po! Ingat po!"
    )
    lang = LC.language_check(answer, "en")
    assert (lang["mixed"], lang["inLanguage"], lang["hangulRuns"]) == (False, True, [])
    number = check_answer(answer, [{"monthlySurplus": 520000}], [])
    assert number.unsupported == []
    for sentence in ("Kumusta po!", "Salamat po!", "Maraming salamat po!", "Ingat po!"):
        assert classify_v3(sentence) is None

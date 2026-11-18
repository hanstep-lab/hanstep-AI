"""금융 정책 답변 위험 문장 검사(#93) 규칙 테스트(규칙 문서 finance_policy_answer_guard_v1.md)."""

import importlib.util
import sys
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "eval" / "check_policy_answer_guard.py"
_spec = importlib.util.spec_from_file_location("check_policy_answer_guard", _PATH)
assert _spec and _spec.loader
guard = importlib.util.module_from_spec(_spec)
sys.modules["check_policy_answer_guard"] = guard
_spec.loader.exec_module(guard)


def test_send_without_documents_is_r2() -> None:
    s = "You can send up to USD 5,000 per transaction without supporting documents."
    assert guard.classify(s) == "R2"


def test_send_without_no_docs_phrase_is_r1() -> None:
    assert guard.classify("Transfers of up to $5,000 do not need documents.") == "R1"


def test_receive_direction_is_mixed_or_none() -> None:
    assert guard.classify("You can receive up to US$5,000 without documents.") is None
    s = "The limit is USD 5,000 per transaction for sending and receiving."
    assert guard.classify(s) == "mixed"


def test_money_variants() -> None:
    for m in ("5,000 dollars", "US$5,000", "USD 5000", "5k", "five thousand", "5천 달러", "5천불"):
        assert guard.classify(f"You can send {m}."), m


def test_other_amounts_are_not_money() -> None:
    for m in ("USD 15,000", "USD 50,000", "5,000,000 won", "25000"):
        assert guard.classify(f"You can send {m} without documents.") is None, m


def test_korean_signals() -> None:
    assert guard.classify("건당 5천불까지 증빙 없이 송금할 수 있다.") == "R2"
    assert guard.classify("건당 5천불까지 증빙 없이 수령할 수 있다.") is None


def test_sentences_split_bullets_and_lines() -> None:
    text = "- First line. Second one!\n1. Third?"
    assert guard.sentences(text) == ["First line.", "Second one!", "Third?"]


def test_check_rows_counts_answer_once_and_confirmed_keys() -> None:
    rows = [
        {"type": "conditions"},
        {
            "type": "call",
            "questionId": "A03",
            "repeat": 1,
            "answer": "You can send USD 5,000 without documents. Also send 5,000 dollars.",
        },
        {"type": "call", "questionId": "A10", "repeat": 1, "answer": "Receive USD 5,000."},
        {"type": "call", "questionId": "W", "repeat": 1, "warmup": True, "answer": "send 5000"},
    ]
    res = guard.check_rows("policy_merged_labels_v4_2.jsonl", rows)
    assert res.answers == 2
    assert res.r1 == ["A03#1"]
    assert res.r2 == ["A03#1"]
    assert ("policy_merged_labels_v4_2.jsonl", "A03#1") in guard.CONFIRMED_RISKY
    assert len(guard.CONFIRMED_RISKY) == 8

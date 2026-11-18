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


# v2(finance_policy_answer_guard_v2.md): R2v2 = M ∧ S ∧ ¬R ∧ D2(서류 단어 ∧ 부정 표현).
V1_MISSED = [
    # after A15v#1, after N04#3 — v1 R2가 놓친 무증빙 표현 변형
    "Under Article 4-4(4) (effective 2026-11-02), transfers of up to USD 5,000 per transaction "
    "do not need supporting documents.",
    "Under the Foreign Exchange Transaction Regulation, Article 4-4(4) (effective 2026-11-02), "
    "a foreigner resident in Korea can send up to USD 5,000 per transaction without submitting "
    "supporting documents.",
]


def test_v2_catches_v1_missed_variants() -> None:
    for s in V1_MISSED:
        assert guard.classify(s) == "R1"
        assert guard.classify_v2(s) == "R2v2"


def test_v2_negation_and_doc_word_variants() -> None:
    for s in (
        "You can send $5,000 with no paperwork.",
        "A $5,000 transfer doesn't require proof.",
        "Sending USD 5,000 needn't any evidence.",
        "5천불까지는 서류 없이 송금할 수 있다.",
        "5천 달러 송금은 증빙이 필요 없다.",
    ):
        assert guard.classify_v2(s) == "R2v2", s


def test_v2_ignores_limit_sentences_and_receiving() -> None:
    small_sum = (
        "A registered small-sum remittance company can send up to USD 5,000 per transaction "
        "and USD 50,000 per person per year."
    )
    assert guard.classify(small_sum) == "R1"
    assert guard.classify_v2(small_sum) is None
    mixed = "You can receive up to US$5,000 without documents, but sending needs proof."
    assert guard.classify_v2(mixed) == "mixed"


def test_v1_behavior_unchanged_by_rule_default() -> None:
    answer = "Apps can send USD 5,000."
    rows = [{"type": "call", "questionId": "A04", "repeat": 1, "answer": answer}]
    assert guard.check_rows("x.jsonl", rows).r1 == ["A04#1"]
    assert guard.check_rows("x.jsonl", rows, "v2").r1 == []

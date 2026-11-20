"""scripts/_v45_check.py: 프롬프트 v4.5 측정(#114) 채점."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import _v45_check as V  # noqa: E402

pytestmark = pytest.mark.unit

# 로컬 Reyes(2026-11-20): 거래 기준 고정 520,000·송금 700,000·변동 454,683·여유 675,317,
# 입력값 고정 1,250,000·송금 700,000·변동 1,000,000 → 입력값 기준 여유 −600,000.
TOOL_RESULT: dict[str, Any] = {
    "analysis": {
        "avgMonthlyIncome": 2350000,
        "fixedLiving": 520000,
        "remittance": 700000,
        "variableExpense": {"avg": 454683, "min": 1, "max": 2},
        "spare": 675317,
    },
    "declaredCommitments": {
        "fixedLiving": 1250000,
        "remittance": 700000,
        "variableExpense": 1000000,
        "mandatorySaving": 0,
        "deductibleTotal": 2950000,
    },
    "goals": [{"goalType": "LUMP_SUM"}, {"goalType": "CREDIT"}],
}


def _row(kind: str, expect: str | None = None) -> dict[str, Any]:
    return {"v45Check": kind, "v45Expect": expect, "toolResult": TOOL_RESULT}


def test_expected_values_use_input_then_transactions() -> None:
    values = V.expected_values(TOOL_RESULT)
    assert values["input"] == {
        "fixedLiving": 1250000,
        "remittance": 700000,
        "variableExpense": 1000000,
        "spare": -600000,
    }
    assert values["sources"] == dict.fromkeys(V.ITEMS, "USER_INPUT")
    no_input = {**TOOL_RESULT, "declaredCommitments": {"fixedLiving": 0}}
    fallback = V.expected_values(no_input)
    assert fallback["input"]["fixedLiving"] == 520000
    assert fallback["sources"]["fixedLiving"] == "TRANSACTIONS"
    assert fallback["input"]["spare"] == 675317


def test_spare_question_matches_negative_input_spare_and_source() -> None:
    answer = (
        "Based on the amounts you entered, your expenses are 600,000 won more than your income "
        "each month."
    )
    result = V.check(_row("input", "spare"), answer)
    assert result is not None
    assert result["inputMatch"] is True and result["sourceMentioned"] is True
    assert result["usedTransactionNumbers"] is False


def test_transaction_spare_is_flagged() -> None:
    result = V.check(_row("input", "spare"), "You have about 675,000 won left each month.")
    assert result is not None
    assert result["inputMatch"] is False
    assert result["usedTransactionNumbers"] is True and result["transactionHits"] == ["spare"]
    assert result["sourceMentioned"] is False


def test_items_question_needs_all_three_input_values() -> None:
    answer = "Fixed costs 1,250,000 won, remittance 700,000 won, other spending 1,000,000 won."
    result = V.check(_row("input", "items"), answer)
    assert result is not None and result["inputMatch"] is True
    partial = V.check(_row("input", "items"), "Fixed costs 520,000 won, remittance 700,000 won.")
    assert partial is not None and partial["inputMatch"] is False
    # 송금은 입력값과 거래 기준이 같아 거래 기준 사용으로 세지 않는다.
    assert partial["transactionHits"] == ["fixedLiving"]


def test_goal_mentions_ratio_and_order() -> None:
    both = V.check(_row("goals"), "First, build your lump sum. Then work on your credit.")
    assert both == {
        "kind": "goals",
        "goals": ["LUMP_SUM", "CREDIT"],
        "mentioned": ["LUMP_SUM", "CREDIT"],
        "ratio": 1.0,
        "orderOk": True,
    }
    reversed_order = V.check(_row("goals"), "Check your credit, then save up for the lump sum.")
    assert reversed_order is not None and reversed_order["orderOk"] is False
    one = V.check(_row("goals"), "Keep saving up every month.")
    assert one is not None and one["ratio"] == 0.5 and one["orderOk"] is None
    assert V.check({"toolResult": TOOL_RESULT}, "x") is None


def test_summary_lines() -> None:
    rows = [
        {
            "questionId": "V01",
            "status": "done",
            "numberCheck": {"unsupported": []},
            "v45Result": V.check(_row("input", "spare"), "You have 675,317 won left."),
        },
        {
            "questionId": "V03",
            "status": "done",
            "numberCheck": {"unsupported": ["5"]},
            "v45Result": V.check(_row("goals"), "Save up a lump sum, then credit."),
        },
    ]
    text = "\n".join(V.summary_lines(rows))
    assert "| V01 | 1 | 0/1 | 1/1 | 0/1 | 1/1 |" in text
    assert "| V03 | 1 | LUMP_SUM·CREDIT | 1.00 | 1/1 | 1/1 | 0/1 |" in text
    assert V.summary_lines([]) == []

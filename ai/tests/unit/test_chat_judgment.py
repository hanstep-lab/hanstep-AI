"""app/chat/judgment.py with the Minh fixture, and the finance tool with judgment facts on/off."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from app.chat.judgment import SPARE_DEFINITION, judgment_facts, message_amounts, message_months
from app.chat.tools import FIXTURE, FinanceTool, FixtureFinanceContext, summarize

pytestmark = pytest.mark.unit

SUMMARY: dict[str, Any] = summarize(json.loads(FIXTURE.read_text(encoding="utf-8")))
SPARE = 675_317


def _facts(message: str = "", summary: dict[str, Any] | None = None) -> dict[str, Any]:
    facts = judgment_facts(summary or SUMMARY, message)
    assert facts is not None
    return facts


def _derived(facts: dict[str, Any]) -> dict[str, Any]:
    return {d["name"]: (d["value"], d["unit"]) for d in facts["derived"]}


def test_fixed_parts_and_all_derived_values() -> None:
    facts = _facts("How are my savings goals going?")
    assert facts["spareDefinition"] == SPARE_DEFINITION
    assert facts["usualRemittanceIncludedInSpare"] is True
    assert _derived(facts) == {
        "yearlyRemittance": (8_400_000, "KRW"),
        "yearlySpare": (8_103_804, "KRW"),
        "incomeMinusFixed": (1_830_000, "KRW"),
        "variableRange": (101_800, "KRW"),
        "fixedShareOfIncome": (22.1, "percent"),  # 22.13
        "remittanceShareOfIncome": (29.8, "percent"),  # 29.79
        "variableShareOfIncome": (19.3, "percent"),  # 19.35
        "spareShareOfIncome": (28.7, "percent"),  # 28.74
    }
    assert all(set(d) == {"name", "value", "unit", "formula"} for d in facts["derived"])
    assert facts["scenarios"] == []


def test_percent_rounds_half_up() -> None:
    summary = copy.deepcopy(SUMMARY)
    summary["analysis"]["avgMonthlyIncome"] = 2_000_000
    summary["analysis"]["fixedLiving"] = 501  # 0.02505 % -> 0.0
    summary["analysis"]["remittance"] = 1_000  # 0.05 % -> 0.1 (half-up, not banker's 0.0)
    shares = _derived(_facts(summary=summary))
    assert shares["remittanceShareOfIncome"] == (0.1, "percent")
    assert shares["fixedShareOfIncome"] == (0.0, "percent")


def test_one_amount_scenario() -> None:
    [scenario] = _facts("If I send 200,000 won more than usual this month?")["scenarios"]
    assert scenario == {
        "description": (
            "If 200,000 KRW is an additional expense this month on top of the usual monthly "
            "budget (which already includes the usual remittance of 700,000 KRW), spare would "
            "be 475,317 KRW"
        ),
        "value": 475_317,
        "unit": "KRW",
        "formula": "spare - 200000",
    }


def test_several_amounts_and_known_item_note() -> None:
    scenarios = _facts("Will sending 700,000 won home and 1,000,000 KRW more be too much?")[
        "scenarios"
    ]
    assert [(s["value"], s["formula"]) for s in scenarios] == [
        (SPARE - 700_000, "spare - 700000"),
        (SPARE - 1_000_000, "spare - 1000000"),
    ]
    assert scenarios[0]["note"] == (
        "700,000 KRW equals the usual remittance, which is already included in the monthly figures."
    )
    assert "note" not in scenarios[1]


@pytest.mark.parametrize(
    ("amount", "label"),
    [
        ("520,000", "fixed living costs"),
        ("2,350,000", "monthly income"),
        ("454,683", "variable spending"),
    ],  # fmt: skip
)
def test_note_for_every_known_monthly_item(amount: str, label: str) -> None:
    [scenario] = _facts(f"What about {amount} won?")["scenarios"]
    assert scenario["note"].startswith(f"{amount} KRW equals the usual {label}")


def test_amount_rules() -> None:
    # Plain numbers count from 10,000; smaller plain numbers, percents and dates do not.
    assert message_amounts("Can I save 500000 a month?") == [500_000]
    assert message_amounts("Is 9,999 or 30% or 2026-10-31 a problem?") == []
    assert message_amounts("Is 300 won and 2.5 million won ok? 300 won again") == [300, 2_500_000]
    assert message_amounts("약 50만 원을 더 보내면?") == [500_000]
    assert _facts("What is my spare money?")["scenarios"] == []


def test_period_scenarios() -> None:
    assert message_months("for 6 months, 1 month, 6개월, 3달 and 6 months again") == [6, 1, 3]
    scenarios = _facts("If I save all my spare money for 6 months, how much will I have?")[
        "scenarios"
    ]
    assert scenarios == [
        {
            "description": "spare over 6 months = spare × 6",
            "value": 4_051_902,
            "unit": "KRW",
            "formula": "spare * 6",
        }
    ]
    assert _facts("in 1 month")["scenarios"][0]["description"] == "spare over 1 month = spare × 1"


@pytest.mark.parametrize("text", ["0 months", "61 months", "100 months", "120개월", "1.5 months"])
def test_period_out_of_range_is_ignored(text: str) -> None:
    assert message_months(f"What about {text}?") == []


def test_null_values_drop_only_the_facts_that_use_them() -> None:
    summary = copy.deepcopy(SUMMARY)
    summary["analysis"]["remittance"] = None
    summary["analysis"]["variableExpense"]["min"] = None
    facts = _facts("Can I send 200,000 won more?", summary)
    assert set(_derived(facts)) == {
        "yearlySpare",
        "incomeMinusFixed",
        "fixedShareOfIncome",
        "variableShareOfIncome",
        "spareShareOfIncome",
    }
    [scenario] = facts["scenarios"]
    assert "(which already includes the usual remittance)" in scenario["description"]

    summary["analysis"]["spare"] = None
    summary["analysis"]["avgMonthlyIncome"] = None
    facts = _facts("Can I send 200,000 won more for 6 months?", summary)
    assert set(_derived(facts)) == set()
    assert facts["scenarios"] == []


def test_no_analysis_means_no_facts() -> None:
    assert (
        judgment_facts({"analysis": None, "message": "No financial analysis result."}, "x") is None
    )
    assert judgment_facts({}, "x") is None


async def _tool_content(**options: Any) -> str:
    tool = FinanceTool(FixtureFinanceContext(), user_id=1, request_id=None, timeout=5, **options)
    outcome = await tool.run("get_financial_analysis", {})
    assert outcome.status == "SUCCEEDED"
    return outcome.content


async def test_tool_result_unchanged_when_off() -> None:
    expected = json.dumps(SUMMARY, ensure_ascii=False)  # what the tool returned before
    assert await _tool_content() == expected
    assert await _tool_content(message="Can I send 200,000 won more?") == expected
    assert (
        await _tool_content(message="Can I send 200,000 won more?", judgment_facts=False)
        == expected
    )


async def test_tool_result_adds_facts_when_on() -> None:
    message = "If I send 200,000 won more than usual this month, will I still have money left?"
    content = json.loads(await _tool_content(message=message, judgment_facts=True))
    facts = content.pop("judgmentFacts")
    assert content == SUMMARY  # every existing key and value stays
    assert facts == judgment_facts(SUMMARY, message)
    assert facts["scenarios"][0]["value"] == 475_317

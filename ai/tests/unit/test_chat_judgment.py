"""app/chat/judgment.py with the Minh fixture, and the finance tool with judgment facts on/off."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from app.chat.judgment import (
    SPARE_DEFINITION,
    is_recurring,
    judgment_facts,
    message_amounts,
    message_months,
)
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


@pytest.mark.parametrize(
    ("message", "amounts", "months"),
    [
        ("If I spend 200,000 won more this month, is that okay?", [200_000], []),
        ("이번 달에 20만 원 더 쓰면 괜찮아?", [200_000], []),
        ("이번 달 20만원 더 보내도 돼?", [200_000], []),
        ("1.5만 원짜리 구독을 추가해도 될까?", [15_000], []),
        ("Can I spend ₩300,000 extra?", [300_000], []),
        ("How much can I save in a year?", [], [12]),
        ("Can I save it for one year?", [], [12]),
        ("1년 동안 모으면 얼마야?", [], [12]),
        ("What about half a year?", [], [6]),
        ("반년이면?", [], [6]),
        ("In 2 years?", [], [24]),
        ("2년이면?", [], [24]),
        ("6개월 동안 70만 원씩 보내면?", [700_000], [6]),
    ],
)
def test_amount_and_period_forms(message: str, amounts: list[int], months: list[int]) -> None:
    assert message_amounts(message) == amounts
    assert message_months(message) == months


def test_same_amount_or_period_in_several_forms_counts_once() -> None:
    assert message_amounts("20만 원, 그러니까 200,000 won, 20만원") == [200_000]
    assert message_months("1 year, 12 months, 1년, a year, 12개월") == [12]
    assert message_months("half a year or 6 months, 반년") == [6]
    scenarios = _facts("20만 원 (200,000 won) for 1 year / 12 months")["scenarios"]
    assert [s["formula"] for s in scenarios] == ["spare - 200000", "spare * 12"]


@pytest.mark.parametrize(
    "message",
    ["In 6 years?", "10년이면?", "2026년에는?", "2026 year plan", "1.5 years", "0년"],
)
def test_year_periods_out_of_range_are_ignored(message: str) -> None:
    assert message_months(message) == []


def test_man_won_amount_that_is_not_whole_won_is_ignored() -> None:
    assert message_amounts("1.23456만 원이면?") == []
    assert message_amounts("1.2345만 원이면?") == [12_345]


def test_note_applies_to_man_won_amounts() -> None:
    [scenario] = _facts("이번 달에 70만 원 보내도 돼?")["scenarios"]
    assert scenario["value"] == SPARE - 700_000
    assert scenario["note"].startswith("700,000 KRW equals the usual remittance")


def _table(message: str) -> list[tuple[str, int]]:
    return [(s["formula"], s["value"]) for s in _facts(message)["scenarios"]]


@pytest.mark.parametrize(
    "message",
    [
        "6개월 동안 70만 원씩 보내면?",
        "매달 70만 원 보내면?",
        "매월 700,000 won",
        "달마다 70만원",
        "I send 700,000 won every month",
        "700,000 won each month",
        "700,000 won per month",
        "700,000 won a month",
        "monthly 700,000 won",
    ],
)
def test_recurring_markers(message: str) -> None:
    assert is_recurring(message)
    assert all(s["value"] != SPARE - 700_000 for s in _facts(message)["scenarios"])


def test_recurring_amount_equal_to_a_monthly_item_is_not_taken_off_again() -> None:
    scenarios = _facts("6개월 동안 70만 원씩 보내면?")["scenarios"]
    assert scenarios[0] == {
        "description": (
            "700,000 KRW per month equals the usual remittance, which is already included in the "
            "monthly figures, so spare stays 675,317 KRW per month"
        ),
        "value": SPARE,
        "unit": "KRW",
        "formula": "spare",
    }
    assert _table("6개월 동안 70만 원씩 보내면?") == [("spare", SPARE), ("spare * 6", 4_051_902)]
    assert -24_683 not in [s["value"] for s in scenarios]


def test_recurring_amount_with_a_period() -> None:
    scenarios = _facts("매달 30만 원씩 6개월 모으면?")["scenarios"]
    assert _table("매달 30만 원씩 6개월 모으면?") == [
        ("spare - 300000", 375_317),
        ("300000 * 6", 1_800_000),
        ("(spare - 300000) * 6", 2_251_902),
        ("spare * 6", 4_051_902),
    ]
    assert [s["description"] for s in scenarios[:3]] == [
        "If 300,000 KRW is spent or set aside every month on top of the usual monthly budget, "
        "spare each month would be 375,317 KRW",
        "300,000 KRW every month for 6 months totals 1,800,000 KRW",
        "spare left over 6 months would be 2,251,902 KRW",
    ]


@pytest.mark.parametrize(
    ("message", "table"),
    [
        ("I want to save 100,000 won a month", [("spare - 100000", 575_317)]),
        ("monthly 50,000 won subscription", [("spare - 50000", 625_317)]),
    ],
)
def test_recurring_amount_without_a_period(message: str, table: list[tuple[str, int]]) -> None:
    assert _table(message) == table


@pytest.mark.parametrize(
    "message",
    [
        "If I spend 200,000 won more this month, is that okay?",
        "이번 달에 20만 원 더 쓰면 괜찮아?",
        "Can I spend ₩300,000 extra?",
        "1.5만 원짜리 구독을 추가해도 될까?",
    ],
)
def test_no_recurring_marker_keeps_the_one_off_scenario(message: str) -> None:
    assert not is_recurring(message)
    [scenario] = _facts(message)["scenarios"]
    assert "is an additional expense this month" in scenario["description"]

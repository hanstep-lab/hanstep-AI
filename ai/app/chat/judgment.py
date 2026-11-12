"""Judgment facts for the finance tool result (CHAT_JUDGMENT_FACTS): numbers computed by code.

Pure functions. Input: the ``summarize()`` result and this turn's user message. Output: the
``judgmentFacts`` object the LLM can quote instead of calculating itself, or None when there is
no analysis. Only int and Decimal arithmetic; percents are rounded half-up to one decimal and
turned into a JSON number only at the end. A value that is null drops only the facts that use it.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.chat.number_check import scan_numbers

SPARE_DEFINITION = (
    "spare = avgMonthlyIncome - fixedLiving - remittance - variableExpense.avg. The usual "
    "remittance is already subtracted, so spare is what is left after sending the usual amount "
    "home."
)
MIN_PLAIN_AMOUNT = 10_000  # a number without a currency counts as an amount from here
MAX_MONTHS = 60
_PERIOD = re.compile(r"(?<![\d,.])(\d{1,2})\s*(?:months?\b|개월|달)", re.IGNORECASE)
# Monthly items an amount in the message may simply repeat (the same money, not extra money).
_KNOWN_ITEMS = (
    ("remittance", "remittance"),
    ("fixedLiving", "fixed living costs"),
    ("avgMonthlyIncome", "monthly income"),
    ("variableExpense.avg", "variable spending"),
)


def _percent(part: int, whole: int) -> Decimal:
    return (Decimal(part) * 100 / Decimal(whole)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def _json_number(value: int | Decimal) -> int | float:
    # The only conversion out of Decimal: the result is already rounded to one decimal.
    return value if isinstance(value, int) else float(value)


def _krw(value: int) -> str:
    return f"{value:,}"


def _values(analysis: dict[str, Any]) -> dict[str, int | None]:
    variable = analysis.get("variableExpense") or {}
    return {
        "avgMonthlyIncome": analysis.get("avgMonthlyIncome"),
        "fixedLiving": analysis.get("fixedLiving"),
        "remittance": analysis.get("remittance"),
        "variableExpense.avg": variable.get("avg"),
        "variableExpense.min": variable.get("min"),
        "variableExpense.max": variable.get("max"),
        "spare": analysis.get("spare"),
    }


def derived_facts(values: dict[str, int | None]) -> list[dict[str, Any]]:
    income = values["avgMonthlyIncome"]
    fixed = values["fixedLiving"]
    remittance = values["remittance"]
    spare = values["spare"]
    low, high = values["variableExpense.min"], values["variableExpense.max"]
    out: list[dict[str, Any]] = []

    def add(name: str, value: int | Decimal, unit: str, formula: str) -> None:
        out.append({"name": name, "value": _json_number(value), "unit": unit, "formula": formula})

    if remittance is not None:
        add("yearlyRemittance", remittance * 12, "KRW", "remittance * 12")
    if spare is not None:
        add("yearlySpare", spare * 12, "KRW", "spare * 12")
    if income is not None and fixed is not None:
        add("incomeMinusFixed", income - fixed, "KRW", "avgMonthlyIncome - fixedLiving")
    if low is not None and high is not None:
        add("variableRange", high - low, "KRW", "variableExpense.max - variableExpense.min")
    if income:  # None or 0: no share of income
        for name, key in (
            ("fixedShareOfIncome", "fixedLiving"),
            ("remittanceShareOfIncome", "remittance"),
            ("variableShareOfIncome", "variableExpense.avg"),
            ("spareShareOfIncome", "spare"),
        ):
            part = values[key]
            if part is not None:
                add(name, _percent(part, income), "percent", f"{key} / avgMonthlyIncome * 100")
    return out


def message_amounts(message: str) -> list[int]:
    """KRW amounts in the message: amounts with a currency or unit, or plain numbers >= 10,000."""
    amounts: list[int] = []
    for _, value, kind, _ in scan_numbers(message):
        if value is None or value != value.to_integral_value():
            continue
        if kind == "amount" or (kind == "number" and value >= MIN_PLAIN_AMOUNT):
            amount = int(value)
            if amount > 0 and amount not in amounts:
                amounts.append(amount)
    return amounts


def message_months(message: str) -> list[int]:
    """Periods like "6 months", "6개월", "6달" (1 to 60)."""
    months: list[int] = []
    for match in _PERIOD.finditer(message):
        n = int(match.group(1))
        if 1 <= n <= MAX_MONTHS and n not in months:
            months.append(n)
    return months


def scenario_facts(values: dict[str, int | None], message: str) -> list[dict[str, Any]]:
    spare = values["spare"]
    if spare is None:
        return []
    remittance = values["remittance"]
    budget = (
        "the usual monthly budget (which already includes the usual remittance"
        + ("" if remittance is None else f" of {_krw(remittance)} KRW")
        + ")"
    )
    out: list[dict[str, Any]] = []
    for amount in message_amounts(message):
        left = spare - amount
        scenario: dict[str, Any] = {
            "description": (
                f"If {_krw(amount)} KRW is an additional expense this month on top of {budget}, "
                f"spare would be {_krw(left)} KRW"
            ),
            "value": left,
            "unit": "KRW",
            "formula": f"spare - {amount}",
        }
        for key, label in _KNOWN_ITEMS:
            if values[key] == amount:
                scenario["note"] = (
                    f"{_krw(amount)} KRW equals the usual {label}, which is already included in "
                    "the monthly figures."
                )
                break
        out.append(scenario)
    for n in message_months(message):
        out.append(
            {
                "description": f"spare over {n} month{'s' if n > 1 else ''} = spare × {n}",
                "value": spare * n,
                "unit": "KRW",
                "formula": f"spare * {n}",
            }
        )
    return out


def judgment_facts(summary: dict[str, Any], message: str) -> dict[str, Any] | None:
    """``judgmentFacts`` for a ``summarize()`` result, or None when there is no analysis."""
    analysis = summary.get("analysis")
    if not analysis:
        return None
    values = _values(analysis)
    return {
        "spareDefinition": SPARE_DEFINITION,
        "usualRemittanceIncludedInSpare": True,
        "derived": derived_facts(values),
        "scenarios": scenario_facts(values, message),
    }

"""Naive monthly sum baseline. 비교·평가용, 운영 API에서 사용하지 않음.

No exclusion, classification or duplicate handling: every INCOME row is income and every
EXPENSE row is spending, divided by the number of calendar months the period touches
(partial months included). It shows what the rule engine protects against (e.g. a card
settlement subtracted twice).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class NaiveSummary:
    income: int
    expense: int
    spare: int


def _half_up_div(numerator: int, denominator: int) -> int:
    # Integer HALF_UP for non-negative numerators (amounts are always positive).
    return (2 * numerator + denominator) // (2 * denominator)


def months_touched(period_from: date, period_to: date) -> int:
    return (period_to.year - period_from.year) * 12 + period_to.month - period_from.month + 1


def naive_summary(
    period_from: date, period_to: date, transactions: Iterable[tuple[str, int]]
) -> NaiveSummary:
    """``transactions`` are ``(direction, amount)`` pairs, direction INCOME or EXPENSE."""
    months = months_touched(period_from, period_to)
    income_total = 0
    expense_total = 0
    for direction, amount in transactions:
        if direction == "INCOME":
            income_total += amount
        else:
            expense_total += amount
    income = _half_up_div(income_total, months)
    expense = _half_up_div(expense_total, months)
    return NaiveSummary(income=income, expense=expense, spare=income - expense)

"""단순 월 합계 기준선. 비교·평가용, 운영 API에서 사용하지 않음.

제외, 분류, 중복 처리를 하지 않는다. INCOME 행은 모두 소득, EXPENSE 행은 모두 지출로 보고,
기간이 걸친 달력상 달 수(일부만 덮인 달 포함)로 나눈다. 규칙 엔진이 무엇을 막아 주는지
보여 준다(예: 카드 대금이 두 번 빠지는 문제).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class NaiveSummary:
    """단순 합계 결과: 월 평균 소득, 월 평균 지출, 여유 자금."""

    income: int
    expense: int
    spare: int


def _half_up_div(numerator: int, denominator: int) -> int:
    # 음수가 아닌 분자용 정수 HALF_UP 반올림(금액은 항상 양수다).
    return (2 * numerator + denominator) // (2 * denominator)


def months_touched(period_from: date, period_to: date) -> int:
    return (period_to.year - period_from.year) * 12 + period_to.month - period_from.month + 1


def naive_summary(
    period_from: date, period_to: date, transactions: Iterable[tuple[str, int]]
) -> NaiveSummary:
    """``transactions``는 ``(direction, amount)`` 쌍이고, direction은 INCOME 또는 EXPENSE다."""
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

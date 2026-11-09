"""Rules 5-7: monthly totals with pandas, integer averages and spare money.

All money stays integer (int64 in pandas, int in Python). Averages divide the summed monthly
totals once and round HALF_UP with integer arithmetic; no float mean is ever taken.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date

import pandas as pd

from app.finance.classifier import Bucket, classify
from app.finance.normalizer import normalize
from app.finance.schemas import (
    DataQuality,
    ExcludedTransaction,
    FinanceAnalyzeRequest,
    FinanceAnalyzeResponse,
    Money,
    TransactionIn,
    VariableExpense,
)

NO_FULL_MONTH = "NO_FULL_MONTH"
BUCKETS = [b.value for b in Bucket]


@dataclass(frozen=True)
class MonthSplit:
    full: list[str]
    partial: list[str]


def half_up_div(numerator: int, denominator: int) -> int:
    """Integer division rounded HALF_UP (away from zero on .5)."""
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    quotient, remainder = divmod(abs(numerator), denominator)
    if 2 * remainder >= denominator:
        quotient += 1
    return quotient if numerator >= 0 else -quotient


def month_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def split_months(period_from: date, period_to: date) -> MonthSplit:
    """Months fully covered by the period (1st through last day) versus partially covered."""
    full: list[str] = []
    partial: list[str] = []
    year, month = period_from.year, period_from.month
    while (year, month) <= (period_to.year, period_to.month):
        first = date(year, month, 1)
        last = date(year, month, calendar.monthrange(year, month)[1])
        key = month_key(first)
        (full if period_from <= first and last <= period_to else partial).append(key)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return MonthSplit(full=full, partial=partial)


def monthly_totals(
    transactions: list[tuple[TransactionIn, Bucket]], months: list[str]
) -> pd.DataFrame:
    """Rows: the given months (zero-filled), columns: buckets, values: int64 sums."""
    frame = pd.DataFrame(
        {
            "month": [month_key(tx.booked_date) for tx, _ in transactions],
            "bucket": [bucket.value for _, bucket in transactions],
            "amount": pd.Series([tx.amount for tx, _ in transactions], dtype="int64"),
        }
    )
    frame = frame[frame["month"].isin(months)]
    if frame.empty:
        return pd.DataFrame(0, index=pd.Index(months), columns=BUCKETS, dtype="int64")
    totals = frame.groupby(["month", "bucket"])["amount"].sum().unstack(fill_value=0)
    return totals.reindex(index=months, columns=BUCKETS, fill_value=0).astype("int64")


def _money(amount: int) -> Money:
    return Money(amount=amount)


def analyze(request: FinanceAnalyzeRequest) -> FinanceAnalyzeResponse:
    """INT-01 analysis. Raises ``FinanceValidationError`` on rule 1 violations."""
    normalized = normalize(request)
    classified: list[tuple[TransactionIn, Bucket]] = []
    unknown: set[str] = set()
    for tx in normalized.kept:
        bucket, unknown_code = classify(tx)
        classified.append((tx, bucket))
        if unknown_code is not None:
            unknown.add(unknown_code)

    months = split_months(request.period_from, request.period_to)
    warnings = {f"UNKNOWN_CATEGORY:{code}" for code in unknown}
    excluded = [
        ExcludedTransaction(transaction_id=e.transaction_id, reason=e.reason)
        for e in normalized.excluded
    ]
    observed = len(months.full)
    if observed == 0:
        warnings.add(NO_FULL_MONTH)
        return FinanceAnalyzeResponse(
            avg_monthly_income=None,
            fixed_living=None,
            remittance=None,
            variable_expense=None,
            spare=None,
            data_quality=DataQuality(
                observed_month_count=0,
                missing_months=[],
                partial_months=months.partial,
                warnings=sorted(warnings),
            ),
            excluded=excluded,
        )

    totals = monthly_totals(classified, months.full)

    def avg(bucket: Bucket) -> int:
        return half_up_div(int(totals[bucket.value].sum()), observed)

    income = avg(Bucket.INCOME)
    fixed = avg(Bucket.FIXED)
    remittance = avg(Bucket.REMITTANCE)
    variable_avg = avg(Bucket.VARIABLE)
    variable = totals[Bucket.VARIABLE.value]
    return FinanceAnalyzeResponse(
        avg_monthly_income=_money(income),
        fixed_living=_money(fixed),
        remittance=_money(remittance),
        variable_expense=VariableExpense(
            avg=_money(variable_avg),
            min=_money(int(variable.min())),
            max=_money(int(variable.max())),
        ),
        spare=_money(income - fixed - remittance - variable_avg),
        data_quality=DataQuality(
            observed_month_count=observed,
            missing_months=[],
            partial_months=months.partial,
            warnings=sorted(warnings),
        ),
        excluded=excluded,
    )

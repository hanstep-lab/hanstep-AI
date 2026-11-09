"""Rule 4: classify kept transactions into income / fixed / remittance / variable."""

from __future__ import annotations

from enum import StrEnum

from app.finance.schemas import Direction, TransactionIn, TransactionKind

FIXED_CATEGORIES = frozenset({"HOUSING", "UTILITY", "TELECOM"})
REMITTANCE_CATEGORY = "REMITTANCE"
VARIABLE_CATEGORIES = frozenset({"FOOD", "TRANSPORT", "LIVING"})


class Bucket(StrEnum):
    INCOME = "INCOME"
    FIXED = "FIXED"
    REMITTANCE = "REMITTANCE"
    VARIABLE = "VARIABLE"


def classify(tx: TransactionIn) -> tuple[Bucket, str | None]:
    """Return the bucket and, for an unrecognized expense category, that category code."""
    if tx.direction is Direction.INCOME:
        return Bucket.INCOME, None
    if tx.transaction_kind is TransactionKind.LOAN_REPAYMENT:
        return Bucket.FIXED, None
    if tx.category_code in FIXED_CATEGORIES:
        return Bucket.FIXED, None
    if tx.category_code == REMITTANCE_CATEGORY:
        return Bucket.REMITTANCE, None
    if tx.category_code in VARIABLE_CATEGORIES:
        return Bucket.VARIABLE, None
    return Bucket.VARIABLE, tx.category_code

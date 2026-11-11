"""규칙 4: 제외되지 않은 거래를 소득 / 고정지출 / 송금 / 변동지출로 분류한다."""

from __future__ import annotations

from enum import StrEnum

from app.finance.schemas import Direction, TransactionIn, TransactionKind

FIXED_CATEGORIES = frozenset({"HOUSING", "UTILITY", "TELECOM"})
REMITTANCE_CATEGORY = "REMITTANCE"
VARIABLE_CATEGORIES = frozenset({"FOOD", "TRANSPORT", "LIVING"})


class Bucket(StrEnum):
    """거래 분류: 소득, 고정지출, 송금, 변동지출."""

    INCOME = "INCOME"
    FIXED = "FIXED"
    REMITTANCE = "REMITTANCE"
    VARIABLE = "VARIABLE"


def classify(tx: TransactionIn) -> tuple[Bucket, str | None]:
    """분류를 돌려준다. 모르는 지출 카테고리면 그 카테고리 코드도 함께 돌려준다."""
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

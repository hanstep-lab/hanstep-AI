"""INT-01 ``POST /internal/v1/finance/analyze`` request and response models (camelCase JSON)."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

# Sanity bound per transaction (1조 원). Keeps int64 sums far from overflow.
MAX_AMOUNT = 1_000_000_000_000
# Generous bound for 12 months of transactions; caps pandas memory if the caller misbehaves.
MAX_TRANSACTIONS = 20_000


class CamelRequest(BaseModel):
    # Same convention as the visa policy API: camelCase only, unknown fields rejected.
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=False, extra="forbid")


class CamelResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class Direction(StrEnum):
    INCOME = "INCOME"
    EXPENSE = "EXPENSE"


class TransactionKind(StrEnum):
    NORMAL = "NORMAL"
    OWN_TRANSFER = "OWN_TRANSFER"
    SAVINGS_TRANSFER = "SAVINGS_TRANSFER"
    LOAN_DISBURSEMENT = "LOAN_DISBURSEMENT"
    LOAN_REPAYMENT = "LOAN_REPAYMENT"
    CARD_SETTLEMENT = "CARD_SETTLEMENT"


class TransactionIn(CamelRequest):
    id: int
    booked_date: date
    direction: Direction
    amount: int = Field(gt=0, le=MAX_AMOUNT, description="KRW, positive integer")
    category_code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z_]+$")
    transaction_kind: TransactionKind


class FinanceAnalyzeRequest(CamelRequest):
    user_id: int
    period_from: date
    period_to: date
    transactions: list[TransactionIn] = Field(max_length=MAX_TRANSACTIONS)


class Money(CamelResponse):
    amount: int
    currency: Literal["KRW"] = "KRW"


class VariableExpense(CamelResponse):
    avg: Money
    min: Money
    max: Money


class DataQuality(CamelResponse):
    observed_month_count: int
    missing_months: list[str]
    partial_months: list[str]
    warnings: list[str]


class ExcludedTransaction(CamelResponse):
    transaction_id: int
    reason: str


class FinanceAnalyzeResponse(CamelResponse):
    avg_monthly_income: Money | None
    fixed_living: Money | None
    remittance: Money | None
    variable_expense: VariableExpense | None
    spare: Money | None
    data_quality: DataQuality
    excluded: list[ExcludedTransaction]

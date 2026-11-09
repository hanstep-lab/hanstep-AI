"""Request checks (rule 1), duplicate ids (rule 2) and excluded transactions (rule 3)."""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date

from app.finance.schemas import FinanceAnalyzeRequest, TransactionIn, TransactionKind

MAX_PERIOD_MONTHS = 12
DUPLICATE_ID = "DUPLICATE_ID"
NEUTRAL = "NEUTRAL"
EXCLUDED_KINDS = frozenset(
    {
        TransactionKind.CARD_SETTLEMENT,
        TransactionKind.OWN_TRANSFER,
        TransactionKind.SAVINGS_TRANSFER,
        TransactionKind.LOAN_DISBURSEMENT,
    }
)


@dataclass(frozen=True)
class FieldError:
    field: str
    reason: str


class FinanceValidationError(ValueError):
    """Rule 1 violation. Carries field paths and reasons only, never submitted values."""

    def __init__(self, errors: list[FieldError]) -> None:
        super().__init__("invalid finance analysis request")
        self.errors = errors


@dataclass(frozen=True)
class Exclusion:
    transaction_id: int
    reason: str
    amount: int


@dataclass
class Normalized:
    kept: list[TransactionIn] = field(default_factory=list)
    excluded: list[Exclusion] = field(default_factory=list)


def add_months(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    last = calendar.monthrange(year, month + 1)[1]
    return date(year, month + 1, min(day.day, last))


def validate(request: FinanceAnalyzeRequest) -> None:
    errors: list[FieldError] = []
    if request.period_from > request.period_to:
        errors.append(FieldError("periodTo", "BEFORE_PERIOD_FROM"))
    elif request.period_to >= add_months(request.period_from, MAX_PERIOD_MONTHS):
        errors.append(FieldError("periodTo", "PERIOD_OVER_12_MONTHS"))

    first_seen: dict[int, TransactionIn] = {}
    for index, tx in enumerate(request.transactions):
        if not request.period_from <= tx.booked_date <= request.period_to:
            errors.append(FieldError(f"transactions.{index}.bookedDate", "OUT_OF_PERIOD"))
        previous = first_seen.setdefault(tx.id, tx)
        if previous is not tx and previous != tx:
            errors.append(FieldError(f"transactions.{index}.id", "CONFLICTING_DUPLICATE_ID"))
    if errors:
        raise FinanceValidationError(errors)


def exclusion_reason(tx: TransactionIn) -> str | None:
    if tx.transaction_kind in EXCLUDED_KINDS:
        return tx.transaction_kind.value
    if tx.category_code == NEUTRAL:
        return NEUTRAL
    return None


def normalize(request: FinanceAnalyzeRequest) -> Normalized:
    """Validate, then split transactions into kept ones and exclusions with reasons."""
    validate(request)
    result = Normalized()
    seen: set[int] = set()
    for tx in request.transactions:
        if tx.id in seen:
            result.excluded.append(Exclusion(tx.id, DUPLICATE_ID, tx.amount))
            continue
        seen.add(tx.id)
        reason = exclusion_reason(tx)
        if reason is None:
            result.kept.append(tx)
        else:
            result.excluded.append(Exclusion(tx.id, reason, tx.amount))
    result.excluded.sort(key=lambda e: (e.transaction_id, e.reason))
    return result

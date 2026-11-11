"""INT-01 ``POST /internal/v1/finance/analyze`` 요청·응답 모델(camelCase JSON)."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

# 거래 1건 금액의 상식선 상한(1조 원). int64 합계가 오버플로 근처에도 가지 않게 한다.
MAX_AMOUNT = 1_000_000_000_000
# 12개월 거래로는 넉넉한 상한. 호출 측이 잘못 보내도 pandas 메모리 사용을 제한한다.
MAX_TRANSACTIONS = 20_000


class CamelRequest(BaseModel):
    """요청 모델 공통 설정."""

    # 비자 정책 API와 같은 규칙: camelCase만 받고 모르는 필드는 거부한다.
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=False, extra="forbid")


class CamelResponse(BaseModel):
    """응답 모델 공통 설정: Python 필드 이름으로 만들고 camelCase로 내보낸다."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class Direction(StrEnum):
    """거래 방향: 수입 또는 지출."""

    INCOME = "INCOME"
    EXPENSE = "EXPENSE"


class TransactionKind(StrEnum):
    """거래 유형. 제외 판정(규칙 3)과 대출 상환 분류(규칙 4)에 쓴다."""

    NORMAL = "NORMAL"
    OWN_TRANSFER = "OWN_TRANSFER"
    SAVINGS_TRANSFER = "SAVINGS_TRANSFER"
    LOAN_DISBURSEMENT = "LOAN_DISBURSEMENT"
    LOAN_REPAYMENT = "LOAN_REPAYMENT"
    CARD_SETTLEMENT = "CARD_SETTLEMENT"


class TransactionIn(CamelRequest):
    """분석 요청에 담긴 거래 한 건."""

    id: int
    booked_date: date
    direction: Direction
    amount: int = Field(gt=0, le=MAX_AMOUNT, description="KRW, positive integer")
    category_code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z_]+$")
    transaction_kind: TransactionKind


class FinanceAnalyzeRequest(CamelRequest):
    """INT-01 분석 요청: 사용자, 분석 기간, 기간 안의 거래 목록."""

    user_id: int
    period_from: date
    period_to: date
    transactions: list[TransactionIn] = Field(max_length=MAX_TRANSACTIONS)


class Money(CamelResponse):
    """금액(원 단위 정수, 통화는 KRW 고정)."""

    amount: int
    currency: Literal["KRW"] = "KRW"


class VariableExpense(CamelResponse):
    """변동지출 요약: 꽉 찬 달의 월 평균과 월별 합계 최소·최대."""

    avg: Money
    min: Money
    max: Money


class DataQuality(CamelResponse):
    """데이터 품질: 평균에 쓴 달 수, 누락된 달, 일부만 덮인 달, 경고."""

    observed_month_count: int
    missing_months: list[str]
    partial_months: list[str]
    warnings: list[str]


class ExcludedTransaction(CamelResponse):
    """집계에서 뺀 거래와 그 이유."""

    transaction_id: int
    reason: str


class FinanceAnalyzeResponse(CamelResponse):
    """INT-01 분석 응답. 꽉 찬 달이 없으면 금액 필드는 모두 null이다."""

    avg_monthly_income: Money | None
    fixed_living: Money | None
    remittance: Money | None
    variable_expense: VariableExpense | None
    spare: Money | None
    data_quality: DataQuality
    excluded: list[ExcludedTransaction]

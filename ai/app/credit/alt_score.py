"""외국인 대안신용 V9 모의 사전진단 계산(서빙).

``models/alt_credit/alt_credit_v9.json``만 읽고 numpy와 표준 라이브러리로 계산한다. JSON은
``scripts/build_alt_credit_v9.py``가 원본(``models/alt_credit/v9_source.py``, 신재현 V9)으로
학습해 고정한 계수·점수 척도·가정이다. sklearn·scipy는 쓰지 않는다(정규분포는 NormalDist).

계산식은 원본의 loan_rate·basel_capital·pd_to_score·limit_components·dsr_max_principal·decide와
같고 연산 순서도 맞춰 둔다. 원본 데모 1,000명과의 일치는 tests/credit/test_alt_score_parity.py가
확인한다. 결과는 모의 사전진단이며 실제 대출 승인이나 신용점수가 아니다.

원본에 없는 입력 처리 두 가지(원본 데모 입력은 모두 범위 안이라 일치 결과는 같다):
- 누적 체류가 학습 범위(JSON feature_ranges, 6~120개월) 밖이면 계산하지 않는다(AltCreditRangeError).
- 송금 비율이 REMIT_MAX(0.8)를 넘으면 그 값으로 잘라 계산하고 inputWarnings에 알린다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from statistics import NormalDist
from typing import Any, Self

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

MODEL_PATH = Path(__file__).resolve().parents[2] / "models" / "alt_credit" / "alt_credit_v9.json"
# 원본 FEATURES 순서. JSON의 features가 이와 다르면 읽지 않는다.
FEATURES = (
    "tel_late_6m",
    "remit_ratio",
    "stay_duration_months",
    "remit_regularity",
    "util_late_6m",
)
DISCLAIMER = "모의 사전진단이며 실제 대출 승인이나 신용점수가 아닙니다"

_NORMAL = NormalDist()


class AltCreditDecision(StrEnum):
    """원본 decide()의 판정. 원본 값: 승인 / 대상아님 / 거절."""

    ELIGIBLE = "ELIGIBLE"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    CONDITION_NOT_MET = "CONDITION_NOT_MET"


class AltCreditReason(StrEnum):
    """원본 decide()의 사유(검사 순서와 같은 순서)."""

    NO_INCOME = "NO_INCOME"  # 소득 없음
    WORK_NOT_PERMITTED = "WORK_NOT_PERMITTED"  # 근로 가능 자격 미확인
    SALARY_UNDER_3M = "SALARY_UNDER_3M"  # 급여 입금 3개월 미만
    EXPIRY_WITHIN_6M = "EXPIRY_WITHIN_6M"  # 체류만료 6개월 이내
    RATE_OVER_LEGAL_MAX = "RATE_OVER_LEGAL_MAX"  # 산출금리가 법정최고금리 초과


class AltCreditWarning(StrEnum):
    """계산은 했지만 입력을 바꿔 쓴 경우."""

    REMIT_RATIO_CLIPPED = "REMIT_RATIO_CLIPPED"  # 송금 비율을 REMIT_MAX로 잘랐다


class AltCreditRangeError(ValueError):
    """입력이 모델 학습 범위 밖이라 계산하지 않는다. 필드 이름과 범위만 담는다(값은 담지 않음)."""

    def __init__(self, field: str, low: float, high: float) -> None:
        super().__init__(f"{field} must be between {low:g} and {high:g} (model training range)")
        self.field = field
        self.low = low
        self.high = high


class LimitBinding(StrEnum):
    """최대한도를 정한 상한. 원본 limit_components()의 키와 순서가 같다."""

    PRODUCT_MAX = "PRODUCT_MAX"  # 상품 최대(3천만)
    ANNUAL_INCOME_80 = "ANNUAL_INCOME_80"  # 연소득×80%
    MONTHLY_PAYMENT_40_TERM = "MONTHLY_PAYMENT_40_TERM"  # 월소득×40%×개월수
    SIMPLE_DSR_40 = "SIMPLE_DSR_40"  # 간이 DSR 40%


@dataclass(frozen=True)
class AltCreditInput:
    """계산 입력. 금액은 원, 기간은 개월."""

    stay_duration_months: int  # 누적 체류 개월
    months_to_expiry: int | None  # 체류 만료까지 남은 개월. None이면 만료 없음(영주 등)
    monthly_income: int
    work_permitted: bool  # 취업 가능 체류자격 또는 시간제취업 허가
    salary_months: int  # 최근 연속 급여 입금 개월 수
    shinhan_payroll: bool  # 신한 급여이체(우대금리 대상)
    has_subscription: bool  # 청약저축 유지(우대금리 대상)
    is_refinance: bool  # 다른 금융기관 대출 대환(우대금리 대상)
    tel_late_6m: int  # 최근 6개월 통신비 연체 횟수(0~6)
    util_late_6m: int  # 최근 6개월 건강보험·공과금 연체 횟수(0~6)
    remit_ratio: float  # 소득 대비 해외송금 비율
    remit_months_6m: int | None  # 최근 6개월 중 송금한 달 수. None이면 송금 안 함 → 중립값
    existing_debt: int = 0
    existing_monthly_payment: int = 0


class _CamelResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class RateBreakdown(_CamelResponse):
    """연 금리(소수). final = base + spread − preferential, spread = 세 가산의 합."""

    base: float = Field(description="기준금리(금융채 6개월)")
    credit_premium: float = Field(description="신용프리미엄(PD × LGD)")
    capital_cost: float = Field(description="자본비용(바젤 필요자본 × 자기자본비용)")
    other_spread: float = Field(description="기타 가산(고정)")
    spread: float = Field(description="가산금리")
    preferential: float = Field(description="우대금리")
    final: float = Field(description="최종 금리")


class AltCreditAssessment(_CamelResponse):
    """모의 사전진단 결과. 실제 대출 승인이나 신용점수가 아니다."""

    model_version: str
    is_mock: bool = Field(description="입력이 Mock 데이터 기반인지(호출부가 지정)")
    pd: float = Field(description="모형 부도확률")
    score: int = Field(description="모의 점수(1~1,000)")
    rate_breakdown: RateBreakdown | None = Field(
        description="소득이 없으면 null(원본도 금리를 내지 않는다)"
    )
    rate_base_date: date = Field(description="기준금리·공시 가산금리 기준일")
    loan_term_months: int = Field(description="대출기간. ELIGIBLE이 아니면 0")
    min_limit: int = Field(description="최소한도(원). ELIGIBLE이 아니면 0")
    max_limit: int = Field(description="최대한도(원). ELIGIBLE이 아니면 0")
    max_limit_binding: LimitBinding | None = Field(description="최대한도를 정한 상한")
    decision: AltCreditDecision
    reason_code: AltCreditReason | None = Field(description="ELIGIBLE이면 null")
    input_warnings: list[AltCreditWarning] = Field(
        description="입력을 바꿔 계산했으면 그 코드. 없으면 빈 목록"
    )
    disclaimer: str


# ---------------------------------------------------------------- 모델 파일(JSON) 계약


class _Assumption(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: bool | int | float | str | list[int]
    basis: str


class _ScoreScale(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    offset: float
    factor: float


class AltCreditModelFile(BaseModel):
    """``alt_credit_v9.json``. 서빙에 쓰지 않는 source·training은 형식만 확인한다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_version: str = Field(min_length=1)
    source: dict[str, str]
    features: list[str]
    coef: list[float]
    intercept: float
    score_scale: _ScoreScale
    remit_regularity_neutral: float = Field(ge=1, le=6)
    # 학습 모집단의 변수별 [최솟값, 최댓값]
    feature_ranges: dict[str, tuple[float, float]]
    assumptions: dict[str, _Assumption]
    training: dict[str, Any]

    @model_validator(mode="after")
    def _check_features(self) -> Self:
        if tuple(self.features) != FEATURES:
            raise ValueError(f"features must be {list(FEATURES)} in this order")
        if len(self.coef) != len(self.features):
            raise ValueError("coef must have one value per feature")
        if set(self.feature_ranges) != set(FEATURES):
            raise ValueError("feature_ranges must have one range per feature")
        if any(low > high for low, high in self.feature_ranges.values()):
            raise ValueError("feature_ranges must be [min, max]")
        return self


@dataclass(frozen=True)
class _Params:
    """계산에 쓰는 가정 값. 괄호 안은 원본 ASSUMPTIONS 이름."""

    lgd: float  # LGD
    hurdle_rate: float  # HURDLE_RATE
    other_spread: float  # OTHER_SPREAD
    base_rate: float  # BASE_RATE
    base_rate_date: date  # BASE_RATE_DATE
    pref_salary: float  # PREF_SALARY
    pref_subscription: float  # PREF_SUBSCRIPTION
    pref_txn_max: float  # PREF_TXN_MAX
    pref_refinance: float  # PREF_REFINANCE
    pref_welcome: float  # PREF_WELCOME
    use_welcome: bool  # USE_WELCOME
    pref_max: float  # PREF_MAX
    max_legal_rate: float  # MAX_LEGAL_RATE
    turnover_rate: float  # TURNOVER_RATE
    payment_ratio: float  # PAYMENT_RATIO
    product_max: int  # PRODUCT_MAX
    income_cap_ratio: float  # INCOME_CAP_RATIO
    limit_unit: int  # LIMIT_UNIT
    max_term: int  # MAX_TERM
    min_remaining: int  # MIN_REMAINING
    term_before_expiry: int  # TERM_BEFORE_EXPIRY
    min_salary_months: int  # MIN_SALARY_MONTHS
    stress_rate_add: float  # STRESS_RATE_ADD
    remit_max: float  # REMIT_MAX(학습 데이터의 송금 비율 상한)


def _params(assumptions: dict[str, _Assumption]) -> _Params:
    def value(name: str) -> bool | int | float | str | list[int]:
        if name not in assumptions:
            raise ValueError(f"assumption {name} is missing")
        return assumptions[name].value

    def number(name: str) -> float:
        # 정수로 적힌 값은 정수 그대로 둔다(원본과 같은 산술).
        v = value(name)
        if isinstance(v, bool) or not isinstance(v, int | float):
            raise ValueError(f"assumption {name} must be a number")
        return v

    def integer(name: str) -> int:
        v = value(name)
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(f"assumption {name} must be an integer")
        return v

    def flag(name: str) -> bool:
        v = value(name)
        if not isinstance(v, bool):
            raise ValueError(f"assumption {name} must be true or false")
        return v

    def day(name: str) -> date:
        v = value(name)
        if not isinstance(v, str):
            raise ValueError(f"assumption {name} must be a date")
        return date.fromisoformat(v)

    return _Params(
        lgd=number("LGD"),
        hurdle_rate=number("HURDLE_RATE"),
        other_spread=number("OTHER_SPREAD"),
        base_rate=number("BASE_RATE"),
        base_rate_date=day("BASE_RATE_DATE"),
        pref_salary=number("PREF_SALARY"),
        pref_subscription=number("PREF_SUBSCRIPTION"),
        pref_txn_max=number("PREF_TXN_MAX"),
        pref_refinance=number("PREF_REFINANCE"),
        pref_welcome=number("PREF_WELCOME"),
        use_welcome=flag("USE_WELCOME"),
        pref_max=number("PREF_MAX"),
        max_legal_rate=number("MAX_LEGAL_RATE"),
        turnover_rate=number("TURNOVER_RATE"),
        payment_ratio=number("PAYMENT_RATIO"),
        product_max=integer("PRODUCT_MAX"),
        income_cap_ratio=number("INCOME_CAP_RATIO"),
        limit_unit=integer("LIMIT_UNIT"),
        max_term=integer("MAX_TERM"),
        min_remaining=integer("MIN_REMAINING"),
        term_before_expiry=integer("TERM_BEFORE_EXPIRY"),
        min_salary_months=integer("MIN_SALARY_MONTHS"),
        stress_rate_add=number("STRESS_RATE_ADD"),
        remit_max=number("REMIT_MAX"),
    )


@dataclass(frozen=True, eq=False)
class AltCreditModel:
    """서빙용으로 읽은 모델. coef는 FEATURES 순서."""

    version: str
    coef: npt.NDArray[np.float64]
    intercept: float
    score_offset: float
    score_factor: float
    remit_regularity_neutral: float
    stay_range: tuple[float, float]  # 누적 체류 학습 범위(개월)
    params: _Params


def load_model(path: Path = MODEL_PATH) -> AltCreditModel:
    """모델 JSON을 읽고 검증한다. 형식이 맞지 않으면 ValueError(pydantic ValidationError 포함)."""
    file = AltCreditModelFile.model_validate_json(path.read_text(encoding="utf-8"))
    return AltCreditModel(
        version=file.model_version,
        coef=np.array(file.coef, dtype=np.float64),
        intercept=file.intercept,
        score_offset=file.score_scale.offset,
        score_factor=file.score_scale.factor,
        remit_regularity_neutral=file.remit_regularity_neutral,
        stay_range=file.feature_ranges["stay_duration_months"],
        params=_params(file.assumptions),
    )


@lru_cache
def default_model() -> AltCreditModel:
    return load_model()


# ---------------------------------------------------------------- 원본과 같은 계산


def _features(inputs: AltCreditInput, neutral: float) -> npt.NDArray[np.float64]:
    # 원본: remit_months_6m.fillna(중립값).round(3). 송금자는 정수라 반올림해도 그대로다.
    months = neutral if inputs.remit_months_6m is None else float(inputs.remit_months_6m)
    values = {
        "tel_late_6m": float(inputs.tel_late_6m),
        "remit_ratio": float(inputs.remit_ratio),
        "stay_duration_months": float(inputs.stay_duration_months),
        "remit_regularity": float(np.round(months, 3)),
        "util_late_6m": float(inputs.util_late_6m),
    }
    return np.array([values[name] for name in FEATURES], dtype=np.float64)


def _probability(model: AltCreditModel, x: npt.NDArray[np.float64]) -> float:
    # sklearn LogisticRegression.predict_proba(이진): expit(X @ coef + intercept)
    z = float(x @ model.coef) + model.intercept
    return 1.0 / (1.0 + math.exp(-z))


def _score(model: AltCreditModel, p: float) -> int:
    # 원본 pd_to_score: clip(offset + factor × ln((1 − p) / p), 1, 1000).round()
    raw = model.score_offset + model.score_factor * np.log((1 - p) / p)
    return int(np.clip(raw, 1, 1000).round())


def _basel_capital(p: float, lgd: float) -> float:
    """원본 basel_capital: 바젤 IRB 필요자본(정규분포는 scipy norm 대신 NormalDist)."""
    p = float(np.clip(p, 1e-5, 0.999))
    w = (1 - np.exp(-35 * p)) / (1 - np.exp(-35))
    r = 0.03 * w + 0.16 * (1 - w)
    z = (_NORMAL.inv_cdf(p) + np.sqrt(r) * _NORMAL.inv_cdf(0.999)) / np.sqrt(1 - r)
    return float(np.maximum(lgd * _NORMAL.cdf(float(z)) - p * lgd, 0))


def _loan_rate(a: _Params, p: float, inputs: AltCreditInput) -> RateBreakdown:
    """원본 loan_rate: 대출금리 = 기준금리 + 가산금리 − 우대금리."""
    credit_premium = p * a.lgd
    capital_cost = _basel_capital(p, a.lgd) * a.hurdle_rate
    spread = credit_premium + capital_cost + a.other_spread
    # 거래실적 우대(급여이체 + 청약, 최고 1.5%)
    txn = min(
        a.pref_salary * int(inputs.shinhan_payroll)
        + a.pref_subscription * int(inputs.has_subscription),
        a.pref_txn_max,
    )
    welcome = a.pref_welcome if a.use_welcome else 0.0
    pref = min(txn + a.pref_refinance * int(inputs.is_refinance) + welcome, a.pref_max)
    return RateBreakdown(
        base=a.base_rate,
        credit_premium=credit_premium,
        capital_cost=capital_cost,
        other_spread=a.other_spread,
        spread=spread,
        preferential=pref,
        final=a.base_rate + spread - pref,
    )


def _dsr_max_principal(
    a: _Params, monthly_income: float, existing_payment: int, annual_rate: float, n: int
) -> float:
    """원본 dsr_max_principal: 원금분할상환 첫 달 상환액이 월소득×40% − 기존 상환액 이하."""
    room = monthly_income * a.payment_ratio - existing_payment
    if room <= 0 or n <= 0:
        return 0.0
    return room / (1 / n + annual_rate / 12)


def _limit(
    a: _Params, monthly_income: float, inputs: AltCreditInput, rate: float, n: int
) -> tuple[int, LimitBinding]:
    """원본 limit_components: 4개 상한 중 가장 작은 값 − 기존 대출, 10만원 단위 절사."""
    caps: dict[LimitBinding, float] = {
        LimitBinding.PRODUCT_MAX: a.product_max,
        LimitBinding.ANNUAL_INCOME_80: monthly_income * 12 * a.income_cap_ratio,
        LimitBinding.MONTHLY_PAYMENT_40_TERM: monthly_income * a.payment_ratio * n,
        LimitBinding.SIMPLE_DSR_40: _dsr_max_principal(
            a, monthly_income, inputs.existing_monthly_payment, rate, n
        ),
    }
    binding = min(caps, key=caps.__getitem__)  # 같은 값이면 앞의 상한(원본 min과 같음)
    limit = max(0.0, caps[binding] - inputs.existing_debt)
    return int(limit // a.limit_unit * a.limit_unit), binding


def assess(
    inputs: AltCreditInput, *, is_mock: bool, model: AltCreditModel | None = None
) -> AltCreditAssessment:
    """모의 사전진단 1건. is_mock은 입력 출처(Mock 데이터인지)에 따라 호출부가 정한다.

    누적 체류가 학습 범위 밖이면 AltCreditRangeError를 올린다.
    """
    m = default_model() if model is None else model
    a = m.params
    low, high = m.stay_range
    if not low <= inputs.stay_duration_months <= high:
        raise AltCreditRangeError("stay_duration_months", low, high)
    warnings: list[AltCreditWarning] = []
    if inputs.remit_ratio > a.remit_max:
        inputs = replace(inputs, remit_ratio=a.remit_max)
        warnings.append(AltCreditWarning.REMIT_RATIO_CLIPPED)
    p = _probability(m, _features(inputs, m.remit_regularity_neutral))
    effective_income = inputs.monthly_income * (1 - a.turnover_rate)
    # 원본은 소득자에게만 금리를 낸다.
    rate = _loan_rate(a, p, inputs) if inputs.monthly_income > 0 else None

    def result(
        decision: AltCreditDecision,
        reason: AltCreditReason | None = None,
        term: int = 0,
        limits: tuple[int, int, LimitBinding | None] = (0, 0, None),
    ) -> AltCreditAssessment:
        return AltCreditAssessment(
            model_version=m.version,
            is_mock=is_mock,
            pd=p,
            score=_score(m, p),
            rate_breakdown=rate,
            rate_base_date=a.base_rate_date,
            loan_term_months=term,
            min_limit=limits[0],
            max_limit=limits[1],
            max_limit_binding=limits[2],
            decision=decision,
            reason_code=reason,
            input_warnings=warnings,
            disclaimer=DISCLAIMER,
        )

    # 원본 decide()와 같은 순서로 검사한다.
    if rate is None or effective_income <= 0:
        return result(AltCreditDecision.NOT_ELIGIBLE, AltCreditReason.NO_INCOME)
    if not inputs.work_permitted:
        return result(AltCreditDecision.NOT_ELIGIBLE, AltCreditReason.WORK_NOT_PERMITTED)
    if inputs.salary_months < a.min_salary_months:
        return result(AltCreditDecision.NOT_ELIGIBLE, AltCreditReason.SALARY_UNDER_3M)
    expiry = inputs.months_to_expiry
    if expiry is not None and expiry <= a.min_remaining:
        return result(AltCreditDecision.CONDITION_NOT_MET, AltCreditReason.EXPIRY_WITHIN_6M)
    if rate.final > a.max_legal_rate:
        return result(AltCreditDecision.CONDITION_NOT_MET, AltCreditReason.RATE_OVER_LEGAL_MAX)
    # 만기 = 체류만료 전월 → 대출기간 = min(36, 남은 개월 − 1)
    term = int(min(a.max_term, a.max_term if expiry is None else expiry - a.term_before_expiry))
    # 최대한도: 실제 소득 · 산출금리 / 최소한도: 이직률 감가 소득 · 금리 상승 가산
    hi, hi_binding = _limit(a, inputs.monthly_income, inputs, rate.final, term)
    lo, _ = _limit(a, effective_income, inputs, rate.final + a.stress_rate_add, term)
    return result(AltCreditDecision.ELIGIBLE, term=term, limits=(min(lo, hi), hi, hi_binding))

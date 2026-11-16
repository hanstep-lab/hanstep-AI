"""대안신용 모의 사전진단 API 입력: Spring이 모아 보내는 값(camelCase JSON).

스키마는 형식과 범위만 검증한다. 가입자격·금리·한도 판단은 ``app.credit.alt_score``가 한다.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.credit.alt_score import AltCreditInput

# 금액 상한(1조 원). 금융 분석 INT-01·E-7-4와 같은 값.
MAX_AMOUNT = 1_000_000_000_000
MAX_MONTHS = 1200

Amount = Annotated[int, Field(ge=0, le=MAX_AMOUNT)]
Months = Annotated[int, Field(ge=0, le=MAX_MONTHS)]
LateCount = Annotated[int, Field(ge=0, le=6)]


class AltCreditRequest(BaseModel):
    """``POST /internal/v1/credit/alt-assessments`` 요청."""

    # 다른 내부 API와 같은 규칙: camelCase만 받고 모르는 필드는 거부한다.
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=False, extra="forbid")

    is_mock: bool = Field(
        description="입력이 Mock 데이터 기반이면 true. 응답 isMock에 그대로 담는다"
    )
    stay_duration_months: Months = Field(description="누적 체류 개월(이전 체류 포함)")
    # 만료 없음과 값 누락을 구분하려고 null도 명시적으로 보내게 한다(기본값 없음).
    months_to_expiry: Months | None = Field(
        description="체류 만료까지 남은 개월(내림). null이면 만료 없음(영주 등)"
    )
    monthly_income: Amount = Field(description="월소득(원)")
    work_permitted: bool = Field(description="취업 가능 체류자격 또는 시간제취업 허가")
    salary_months: Months = Field(description="최근 연속 급여 입금 개월 수")
    shinhan_payroll: bool = Field(description="신한 급여이체 여부(우대금리)")
    has_subscription: bool = Field(description="청약저축 유지 여부(우대금리)")
    is_refinance: bool = Field(description="다른 금융기관 대출 대환 여부(우대금리)")
    # 숫자 뒤는 소문자(telLate6m). to_camel은 telLate6M을 만들어 별칭을 직접 적는다(E-7-4와 같음).
    tel_late_6m: LateCount = Field(alias="telLate6m", description="최근 6개월 통신비 연체 횟수")
    util_late_6m: LateCount = Field(
        alias="utilLate6m", description="최근 6개월 건강보험·공과금 연체 횟수"
    )
    # 원본 학습 데이터의 범위는 0~0.8(REMIT_MAX). 0.8 초과도 받아서 그대로 계산한다.
    remit_ratio: float = Field(
        ge=0, le=1, allow_inf_nan=False, description="소득 대비 해외송금 비율"
    )
    remit_months_6m: Annotated[int, Field(ge=1, le=6)] | None = Field(
        alias="remitMonths6m",
        description="최근 6개월 중 해외송금한 달 수. 송금하지 않았으면 null(중립값으로 계산)",
    )
    existing_debt: Amount = Field(default=0, description="기존 대출 잔액(원)")
    existing_monthly_payment: Amount = Field(default=0, description="기존 대출 월상환액(원)")

    def to_input(self) -> AltCreditInput:
        return AltCreditInput(
            stay_duration_months=self.stay_duration_months,
            months_to_expiry=self.months_to_expiry,
            monthly_income=self.monthly_income,
            work_permitted=self.work_permitted,
            salary_months=self.salary_months,
            shinhan_payroll=self.shinhan_payroll,
            has_subscription=self.has_subscription,
            is_refinance=self.is_refinance,
            tel_late_6m=self.tel_late_6m,
            util_late_6m=self.util_late_6m,
            remit_ratio=self.remit_ratio,
            remit_months_6m=self.remit_months_6m,
            existing_debt=self.existing_debt,
            existing_monthly_payment=self.existing_monthly_payment,
        )

"""E-7-4 점수 계산 입력: Spring이 MySQL에서 모아 보내는 프로필 스냅샷(camelCase JSON).

필드 이름과 enum 값은 MySQL DDL(visa_profiles, employment_histories, income_records,
visa_evidence)에 맞춘다. DDL에 없는 값(체류 시작일, 이직 사유, 감점·제외 자기신고 등)은
선택 필드로 받는다. 값이 없으면 엔진이 판정을 보류하고 누락 입력으로 돌려주며,
LLM이 그 목록으로 사용자에게 다시 묻는다.

스키마는 형식만 검증한다. 4년 체류, 3년 근속 같은 업무 판정은 엔진이 한다.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

# 금액 상한(1조 원). 금융 분석 INT-01과 같은 값.
MAX_AMOUNT = 1_000_000_000_000
MAX_EMPLOYMENTS = 50
MAX_INCOMES = 20
MAX_EVIDENCES = 50
# 체류요건은 최근 10년(120개월) 안의 기간만 센다.
MAX_PRIOR_STAY_MONTHS = 120
MAX_DECLARED_COUNT = 99

VISA_CODE_PATTERN = r"^[A-Z]-\d{1,2}(-\d{1,2})?$"


class CamelRequest(BaseModel):
    # 금융 분석·정책 검색 API와 같은 규칙: camelCase만 받고 모르는 필드는 거부한다.
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=False, extra="forbid")


class VerificationStatus(StrEnum):
    """employment_histories·income_records·visa_evidence의 증빙 상태."""

    DECLARED = "DECLARED"
    NEEDS_EVIDENCE = "NEEDS_EVIDENCE"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"


class ProfileEvidenceStatus(StrEnum):
    """visa_profiles.evidence_status. 다른 테이블의 증빙 상태와 값이 다르다."""

    DECLARED = "DECLARED"
    NEEDS_VERIFICATION = "NEEDS_VERIFICATION"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    VERIFIED = "VERIFIED"


class IncomeSource(StrEnum):
    """income_records.source. 거래 합계 추정값은 공식 인정소득이 아니다."""

    USER_DECLARED = "USER_DECLARED"
    DOCUMENT = "DOCUMENT"
    TRANSACTION_ESTIMATE = "TRANSACTION_ESTIMATE"


class EvidenceType(StrEnum):
    """visa_evidence.evidence_type."""

    TOPIK = "TOPIK"
    KIIP = "KIIP"
    KIIP_PRE_TEST = "KIIP_PRE_TEST"
    DRIVER_LICENSE = "DRIVER_LICENSE"
    NATIONAL_CERTIFICATE = "NATIONAL_CERTIFICATE"
    KOREAN_DEGREE = "KOREAN_DEGREE"
    RECOMMENDATION_EMPLOYER = "RECOMMENDATION_EMPLOYER"
    RECOMMENDATION_MINISTRY = "RECOMMENDATION_MINISTRY"
    RECOMMENDATION_PROVINCE = "RECOMMENDATION_PROVINCE"


class VisaProfileIn(CamelRequest):
    visa_type_code: str | None = Field(
        default=None, pattern=VISA_CODE_PATTERN, description="현재 체류자격 코드(예: E-9)"
    )
    birth_date: date | None = Field(default=None, description="생년월일. 나이 점수용")
    evidence_status: ProfileEvidenceStatus = ProfileEvidenceStatus.DECLARED


class StayIn(CamelRequest):
    """체류요건(최근 10년 안에 E-9·E-10·H-2로 합계 4년) 판정용. DDL에 없어 자기신고로 받는다."""

    current_stay_started_on: date | None = Field(
        default=None, description="현재 체류 시작일(입국일). 외국인등록증 발급일과 다를 수 있다"
    )
    prior_stay_months: int | None = Field(
        default=None,
        ge=0,
        le=MAX_PRIOR_STAY_MONTHS,
        description="현재 체류 이전, 최근 10년 안의 E-9·E-10·H-2 체류 기간 합계(개월)",
    )
    verification_status: VerificationStatus = VerificationStatus.DECLARED


class EmploymentIn(CamelRequest):
    """employment_histories 한 행.

    서비스는 E-9 경로만 지원하므로 모든 근무 기간을 E-9·E-10·H-2 자격 기간으로 본다.
    """

    employment_id: int = Field(ge=1)
    employer_key: str = Field(
        min_length=1,
        max_length=64,
        description="같은 사업장 판정용 식별자(Spring 생성). 사업장명 원문은 받지 않는다",
    )
    industry_code: str = Field(min_length=1, max_length=30, pattern=r"^[A-Z_]+$")
    workplace_region_code: str | None = Field(
        default=None, max_length=20, pattern=r"^[0-9]+$", description="사업장 소재 시군구 코드"
    )
    is_depopulation_area: bool | None = Field(
        default=None, description="인구감소지역 또는 읍·면지역 여부(지역 가점)"
    )
    started_on: date
    ended_on: date | None = Field(default=None, description="근무 종료일. 현재 근무처는 null")
    contract_end_on: date | None = Field(default=None, description="고용계약 만료일")
    contract_annual_salary: int | None = Field(
        default=None, ge=0, le=MAX_AMOUNT, description="고용계약 연봉(원)"
    )
    left_without_fault: bool | None = Field(
        default=None,
        description="이 근무처를 휴·폐업, 임금체불 등 근로자 귀책 없는 사유로 떠났는지(DDL 없음)",
    )
    verification_status: VerificationStatus = VerificationStatus.DECLARED

    @model_validator(mode="after")
    def _period_is_ordered(self) -> Self:
        if self.ended_on is not None and self.ended_on < self.started_on:
            raise ValueError("endedOn must not be before startedOn")
        return self


class IncomeIn(CamelRequest):
    """income_records 한 행."""

    income_year: int = Field(ge=2000, le=2100)
    annual_amount: int = Field(ge=0, le=MAX_AMOUNT, description="연간 소득(원)")
    source: IncomeSource
    verification_status: VerificationStatus = VerificationStatus.DECLARED


class EvidenceIn(CamelRequest):
    """visa_evidence 한 행 + DDL에 없는 보조 값."""

    evidence_id: int = Field(ge=1)
    evidence_type: EvidenceType
    level_value: str | None = Field(
        default=None, max_length=30, description="TOPIK 급수, KIIP 단계, 자격명 등"
    )
    issued_on: date | None = None
    expires_on: date | None = None
    status: VerificationStatus = VerificationStatus.DECLARED
    # 아래 세 값은 DDL에 없다(level_value 하나로는 담을 수 없음).
    pre_test_score: int | None = Field(default=None, ge=0, le=100, description="KIIP 사전평가 점수")
    pre_test_assigned_stage: int | None = Field(
        default=None, ge=0, le=5, description="KIIP 사전평가 배정 단계(0단계 배정이면 불인정)"
    )
    related_to_current_job: bool | None = Field(
        default=None, description="자격증이 현재 근무 분야와 관련 있는지"
    )

    @model_validator(mode="after")
    def _validity_is_ordered(self) -> Self:
        if (
            self.issued_on is not None
            and self.expires_on is not None
            and self.expires_on < self.issued_on
        ):
            raise ValueError("expiresOn must not be before issuedOn")
        return self


class LegalDeclarationIn(CamelRequest):
    """감점·제외 판정용 자기신고. 모두 "최근 10년" 기준이며 MySQL에 저장하지 않는다.

    항목마다 null이면 미신고로 보고 엔진이 재질의 대상으로 돌려준다.
    숫자가 든 이름은 to_camel이 `1MCount`처럼 바꾸므로 Jackson과 같은 별칭을 직접 지정한다.
    """

    fines_under_1m_count: int | None = Field(
        default=None,
        alias="finesUnder1mCount",
        ge=0,
        le=MAX_DECLARED_COUNT,
        description="벌금 100만원 미만 형을 받은 횟수",
    )
    has_fine_1m_or_more: bool | None = Field(
        default=None,
        alias="hasFine1mOrMore",
        description="벌금 100만원 이상 형을 받은 적이 있는지",
    )
    tax_stay_restriction_count: int | None = Field(
        default=None,
        ge=0,
        le=MAX_DECLARED_COUNT,
        description="조세 체납으로 체류허가가 제한된 횟수",
    )
    immigration_violation_count: int | None = Field(
        default=None,
        ge=0,
        le=MAX_DECLARED_COUNT,
        description="출입국관리법 위반으로 행정처분(과태료 포함)을 받은 횟수",
    )
    has_overstay_3m_or_more: bool | None = Field(
        default=None,
        alias="hasOverstay3mOrMore",
        description="3개월 이상 불법체류한 적이 있는지",
    )
    has_current_tax_arrears: bool | None = Field(
        default=None, description="현재 체납 중인 세금이 있는지(완납하면 신청 가능)"
    )


class E74Profile(CamelRequest):
    """E-7-4 계산에 쓰는 사용자 프로필 스냅샷."""

    visa_profile: VisaProfileIn
    stay: StayIn | None = None
    employments: list[EmploymentIn] = Field(default_factory=list, max_length=MAX_EMPLOYMENTS)
    incomes: list[IncomeIn] = Field(default_factory=list, max_length=MAX_INCOMES)
    evidences: list[EvidenceIn] = Field(default_factory=list, max_length=MAX_EVIDENCES)
    legal_declaration: LegalDeclarationIn | None = None

    @model_validator(mode="after")
    def _rows_are_consistent(self) -> Self:
        if sum(1 for e in self.employments if e.ended_on is None) > 1:
            raise ValueError("at most one current employment (endedOn null) is allowed")
        _reject_duplicates("employmentId", [e.employment_id for e in self.employments])
        _reject_duplicates("evidenceId", [e.evidence_id for e in self.evidences])
        # MySQL uq_income_records_year(user_id, income_year, source)와 같은 제약.
        _reject_duplicates("incomeYear+source", [(i.income_year, i.source) for i in self.incomes])
        return self

    @property
    def current_employment(self) -> EmploymentIn | None:
        return next((e for e in self.employments if e.ended_on is None), None)


def _reject_duplicates(name: str, keys: list[object]) -> None:
    if len(keys) != len(set(keys)):
        raise ValueError(f"duplicate {name}")

"""E-7-4 규칙 데이터(버전별 JSON) 모델, 로더, 기준일별 버전 선택.

JSON 구조는 MySQL e74_rule_sets·e74_rules와 같다(`condition` = `condition_json`).
숫자는 데이터, 판정 방법은 코드다. 조건마다 `kind`가 있고 엔진은 `kind`별로 판정한다.
정책 숫자가 바뀌면 JSON만 고치고, 판정 방법이 새로 생기면 `kind`를 추가한다.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from app.visa.e74.schemas import EvidenceType

RULE_SETS_DIR = Path(__file__).resolve().parent / "rule_sets"


class RuleSetError(ValueError):
    """규칙 데이터가 잘못됐거나 기준일에 맞는 버전이 모호하다."""


class RuleModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=False, extra="forbid", frozen=True
    )


class RuleCategory(StrEnum):
    """e74_rules.category."""

    BASE_INCOME = "BASE_INCOME"
    BASE_KOREAN = "BASE_KOREAN"
    BASE_AGE = "BASE_AGE"
    BONUS = "BONUS"
    PENALTY = "PENALTY"
    REQUIREMENT = "REQUIREMENT"
    EXCLUSION = "EXCLUSION"


class ApprovalStatus(StrEnum):
    """e74_rule_sets.approval_status. APPROVED만 계산에 쓴다."""

    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    RETIRED = "RETIRED"


class RuleActor(StrEnum):
    """e74_rules.actor: 요건을 채우는 주체."""

    USER = "USER"
    EMPLOYER = "EMPLOYER"
    GOVERNMENT = "GOVERNMENT"


# --- 조건(kind별) ---------------------------------------------------------------


class IncomeRange(RuleModel):
    """평균소득 구간. minAmount 이상, maxAmount 미만(없으면 상한 없음)."""

    kind: Literal["INCOME_RANGE"]
    min_amount: int = Field(ge=0)
    max_amount: int | None = Field(default=None, ge=0)
    industry_codes: tuple[str, ...] | None = None

    @model_validator(mode="after")
    def _range_is_ordered(self) -> Self:
        if self.max_amount is not None and self.max_amount <= self.min_amount:
            raise ValueError("maxAmount must be greater than minAmount")
        return self


class KoreanLevel(RuleModel):
    """한국어 구간. TOPIK 급수, KIIP 이수 단계, KIIP 사전평가 점수 중 하나라도 맞으면 해당."""

    kind: Literal["KOREAN_LEVEL"]
    topik_level_min: int = Field(ge=1, le=6)
    topik_level_max: int | None = Field(default=None, ge=1, le=6)
    kiip_stage_min: int = Field(ge=0, le=5)
    kiip_stage_max: int | None = Field(default=None, ge=0, le=5)
    pre_test_score_min: int = Field(ge=0, le=100)
    pre_test_score_max: int | None = Field(default=None, ge=0, le=100)
    # 사전평가는 이 단계 이상 배정돼야 인정(구술 미달로 0단계 배정되면 불인정).
    pre_test_min_assigned_stage: int = Field(ge=0, le=5)
    pre_test_validity_months: int = Field(ge=1)


class AgeRange(RuleModel):
    """만 나이 구간(양 끝 포함)."""

    kind: Literal["AGE_RANGE"]
    min_age: int = Field(ge=0)
    max_age: int | None = Field(default=None, ge=0)


class EvidencePresent(RuleModel):
    kind: Literal["EVIDENCE_PRESENT"]
    evidence_type: EvidenceType


class Tenure(RuleModel):
    """현재 근무처 근속. 같은 사업장 재고용 기간과 귀책 없는 이직 직전 기간을 합산한다."""

    kind: Literal["TENURE"]
    min_months: int = Field(ge=1)


class RegionWork(RuleModel):
    """최근 lookbackMonths 안에서 인구감소지역·읍면 근무 합계(겹친 기간은 한 번만)."""

    kind: Literal["REGION_WORK"]
    min_months: int = Field(ge=1)
    lookback_months: int = Field(ge=1)


class Qualification(RuleModel):
    """국내 자격증(인정 등급) 또는 국내 학위(인정 과정). 둘 다 있어도 한 번."""

    kind: Literal["QUALIFICATION"]
    certificate_grades: tuple[str, ...] = Field(min_length=1)
    degree_levels: tuple[str, ...] = Field(min_length=1)
    require_job_related: bool


class DriverLicense(RuleModel):
    kind: Literal["DRIVER_LICENSE"]
    accepted_classes: tuple[str, ...] = Field(min_length=1)


PenaltyField = Literal["finesUnder1mCount", "taxStayRestrictionCount", "immigrationViolationCount"]
FlagField = Literal["hasFine1mOrMore", "hasOverstay3mOrMore", "hasCurrentTaxArrears"]


class DeclaredCountPenalty(RuleModel):
    """자기신고 횟수별 감점. n회째 감점 = pointsByCount[n-1], 그 이상은 마지막 값."""

    kind: Literal["DECLARED_COUNT_PENALTY"]
    declaration_field: PenaltyField
    points_by_count: tuple[int, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _schedule_is_positive_and_rising(self) -> Self:
        steps = self.points_by_count
        if any(p <= 0 for p in steps) or any(b < a for a, b in zip(steps, steps[1:], strict=False)):
            raise ValueError("pointsByCount must be positive and non-decreasing")
        return self


class DeclaredFlag(RuleModel):
    """자기신고 예/아니요가 예이면 해당."""

    kind: Literal["DECLARED_FLAG"]
    declaration_field: FlagField


class DeclaredCountAtLeast(RuleModel):
    """자기신고 횟수가 minCount 이상이면 해당."""

    kind: Literal["DECLARED_COUNT_AT_LEAST"]
    declaration_field: PenaltyField
    min_count: int = Field(ge=1)


class ManualReview(RuleModel):
    """심사관 재량 사유. 엔진이 판정하지 않고 안내만 한다."""

    kind: Literal["MANUAL_REVIEW"]


class StayPeriod(RuleModel):
    """최근 lookbackMonths 안에 visaTypes로 합계 minMonths 체류.

    특례: 비수도권(capitalAreaSidoCodes가 아닌 곳)에서 nonCapitalSpecialMinMonths 이상
    체류하고 광역지자체 추천을 받으면 충족.
    """

    kind: Literal["STAY_PERIOD"]
    visa_types: tuple[str, ...] = Field(min_length=1)
    min_months: int = Field(ge=1)
    lookback_months: int = Field(ge=1)
    non_capital_special_min_months: int = Field(ge=1)
    capital_area_sido_codes: tuple[str, ...] = Field(min_length=1)


class CurrentEmployment(RuleModel):
    """현재 체류자격이 visaTypes 중 하나이고 현재 근무처에서 근무 중."""

    kind: Literal["CURRENT_EMPLOYMENT"]
    visa_types: tuple[str, ...] = Field(min_length=1)


class Contract(RuleModel):
    """현재 근무처와 minContractMonths 이상, 연봉 minAnnualSalary 이상 고용계약."""

    kind: Literal["CONTRACT"]
    min_annual_salary: int = Field(ge=0)
    min_contract_months: int = Field(ge=1)
    industry_codes: tuple[str, ...] | None = None


class EmployerRecommendation(RuleModel):
    """현재 minTenureMonths 이상 근무 중인 기업의 추천."""

    kind: Literal["EMPLOYER_RECOMMENDATION"]
    evidence_type: EvidenceType
    min_tenure_months: int = Field(ge=1)


class MinAverageIncome(RuleModel):
    """최근 incomeYears년 평균소득 최소 기준.

    소득 산정 방법(대상 연도, 대체 연도)은 일반 행에만 두고 특례 행은 기준 금액만 가진다.
    전년도 소득금액증명원은 latestYearRequiredFromMonth월부터 요구한다(그 전에는 한 해 전 기준).
    """

    kind: Literal["MIN_AVERAGE_INCOME"]
    min_amount: int = Field(ge=0)
    industry_codes: tuple[str, ...] | None = None
    income_years: int | None = Field(default=None, ge=1)
    latest_year_required_from_month: int | None = Field(default=None, ge=1, le=12)
    fallback_year_from: int | None = None
    fallback_year_to: int | None = None

    @model_validator(mode="after")
    def _general_row_has_basis(self) -> Self:
        basis = (
            self.income_years,
            self.latest_year_required_from_month,
            self.fallback_year_from,
            self.fallback_year_to,
        )
        if self.industry_codes is None and any(v is None for v in basis):
            raise ValueError("the general MIN_AVERAGE_INCOME row needs the income basis fields")
        if self.industry_codes is not None and any(v is not None for v in basis):
            raise ValueError("industry rows take the income basis from the general row")
        return self


class MinKorean(RuleModel):
    """한국어 최소 점수. 유예 기간에는 한국어 외 총점이 기준 이상이면 유예."""

    kind: Literal["MIN_KOREAN"]
    min_points: int = Field(ge=0)
    deferral_until: date | None = None
    deferral_min_total_without_korean: int | None = Field(default=None, ge=0)


class TotalScore(RuleModel):
    """총점이 규칙 버전의 requiredTotalScore 이상."""

    kind: Literal["TOTAL_SCORE"]


Condition = Annotated[
    IncomeRange
    | KoreanLevel
    | AgeRange
    | EvidencePresent
    | Tenure
    | RegionWork
    | Qualification
    | DriverLicense
    | DeclaredCountPenalty
    | DeclaredFlag
    | DeclaredCountAtLeast
    | ManualReview
    | StayPeriod
    | CurrentEmployment
    | Contract
    | EmployerRecommendation
    | MinAverageIncome
    | MinKorean
    | TotalScore,
    Field(discriminator="kind"),
]

# 분류마다 쓸 수 있는 조건 kind.
ALLOWED_KINDS: dict[RuleCategory, frozenset[str]] = {
    RuleCategory.BASE_INCOME: frozenset({"INCOME_RANGE"}),
    RuleCategory.BASE_KOREAN: frozenset({"KOREAN_LEVEL"}),
    RuleCategory.BASE_AGE: frozenset({"AGE_RANGE"}),
    RuleCategory.BONUS: frozenset(
        {"EVIDENCE_PRESENT", "TENURE", "REGION_WORK", "QUALIFICATION", "DRIVER_LICENSE"}
    ),
    RuleCategory.PENALTY: frozenset({"DECLARED_COUNT_PENALTY"}),
    RuleCategory.EXCLUSION: frozenset(
        {"DECLARED_FLAG", "DECLARED_COUNT_AT_LEAST", "MANUAL_REVIEW"}
    ),
    RuleCategory.REQUIREMENT: frozenset(
        {
            "STAY_PERIOD",
            "CURRENT_EMPLOYMENT",
            "CONTRACT",
            "EMPLOYER_RECOMMENDATION",
            "MIN_AVERAGE_INCOME",
            "MIN_KOREAN",
            "TOTAL_SCORE",
        }
    ),
}

SCORED_CATEGORIES = frozenset(
    {
        RuleCategory.BASE_INCOME,
        RuleCategory.BASE_KOREAN,
        RuleCategory.BASE_AGE,
        RuleCategory.BONUS,
    }
)


class Rule(RuleModel):
    """e74_rules 한 행."""

    rule_code: str = Field(pattern=r"^[A-Z0-9_]+$", max_length=60)
    category: RuleCategory
    condition: Condition
    points: int | None = Field(description="배점. 감점은 상한의 음수, 요건·제외는 null")
    exclusive_group: str | None = Field(default=None, max_length=50)
    industry_type: str | None = Field(default=None, max_length=30, pattern=r"^[A-Z_]+$")
    actor: RuleActor = RuleActor.USER
    source_page: str | None = Field(default=None, max_length=50)
    description: str = Field(min_length=1, max_length=300)
    # JSON 전용: 팀 합의 전 해석(정의서 열린 질문). 이 규칙이 적용되면 NEEDS_REVIEW.
    review_note: str | None = Field(default=None, max_length=300)
    # JSON 전용: 증빙 제출 서류(선발계획 붙임2). 개선 방안의 서류 안내에 쓴다.
    documents: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _is_consistent(self) -> Self:
        kind = self.condition.kind
        if kind not in ALLOWED_KINDS[self.category]:
            raise ValueError(f"{self.rule_code}: kind {kind} is not allowed for {self.category}")
        self._check_points()
        has_industry_codes = getattr(self.condition, "industry_codes", None) is not None
        if (self.industry_type is not None) != has_industry_codes:
            raise ValueError(
                f"{self.rule_code}: industryType and condition.industryCodes go together"
            )
        return self

    def _check_points(self) -> None:
        if self.category in SCORED_CATEGORIES:
            if self.points is None or self.points <= 0:
                raise ValueError(f"{self.rule_code}: scored rules need positive points")
        elif self.category is RuleCategory.PENALTY:
            # 감점 행의 kind는 DECLARED_COUNT_PENALTY뿐이다(ALLOWED_KINDS에서 먼저 확인).
            if not isinstance(self.condition, DeclaredCountPenalty) or self.points != -max(
                self.condition.points_by_count
            ):
                raise ValueError(f"{self.rule_code}: penalty points must be -max(pointsByCount)")
        elif self.points is not None:
            raise ValueError(f"{self.rule_code}: requirement and exclusion rules have no points")


class RuleSet(RuleModel):
    """e74_rule_sets 한 행과 그 규칙들."""

    rule_version: str = Field(pattern=r"^E74-\d{4}-v\d+$", max_length=40)
    effective_from: date
    effective_to: date | None = Field(default=None, description="적용 종료(미포함)")
    required_total_score: int | None = Field(default=None, ge=0)
    source_policy_version: str | None = Field(default=None, max_length=40)
    approval_status: ApprovalStatus
    note: str | None = Field(default=None, max_length=500)
    rules: tuple[Rule, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _is_consistent(self) -> Self:
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effectiveTo must be after effectiveFrom")
        codes = [r.rule_code for r in self.rules]
        if len(codes) != len(set(codes)):
            raise ValueError("duplicate ruleCode")
        return self

    def covers(self, as_of: date) -> bool:
        return self.effective_from <= as_of and (
            self.effective_to is None or as_of < self.effective_to
        )

    def rules_in(self, category: RuleCategory) -> tuple[Rule, ...]:
        return tuple(r for r in self.rules if r.category is category)

    def rule(self, rule_code: str) -> Rule:
        for r in self.rules:
            if r.rule_code == rule_code:
                return r
        raise KeyError(rule_code)


# --- 로더와 버전 선택 -------------------------------------------------------------


def load_rule_sets(directory: Path = RULE_SETS_DIR) -> tuple[RuleSet, ...]:
    """디렉터리의 `<ruleVersion>.json`을 모두 읽고 검증한다."""
    rule_sets: list[RuleSet] = []
    for path in sorted(directory.glob("*.json")):
        rule_set = RuleSet.model_validate_json(path.read_text(encoding="utf-8"))
        if path.stem != rule_set.rule_version:
            raise RuleSetError(f"{path.name}: file name must be {rule_set.rule_version}.json")
        rule_sets.append(rule_set)
    versions = [rs.rule_version for rs in rule_sets]
    if len(versions) != len(set(versions)):
        raise RuleSetError("duplicate ruleVersion")
    for status in (ApprovalStatus.APPROVED, ApprovalStatus.DRAFT):
        _reject_overlaps([rs for rs in rule_sets if rs.approval_status is status])
    return tuple(sorted(rule_sets, key=lambda rs: rs.effective_from))


@lru_cache
def default_rule_sets() -> tuple[RuleSet, ...]:
    return load_rule_sets()


def select_rule_set(
    as_of: date, rule_sets: Sequence[RuleSet], *, include_draft: bool = False
) -> RuleSet | None:
    """기준일에 적용할 규칙 버전. APPROVED를 먼저 찾고, 허용할 때만 DRAFT를 쓴다.

    RETIRED는 고르지 않는다. 맞는 버전이 없으면 None(엔진이 NEEDS_REVIEW로 처리).
    """
    statuses = [ApprovalStatus.APPROVED]
    if include_draft:
        statuses.append(ApprovalStatus.DRAFT)
    for status in statuses:
        matches = [rs for rs in rule_sets if rs.approval_status is status and rs.covers(as_of)]
        if len(matches) > 1:
            raise RuleSetError(f"{as_of}: more than one {status} rule set applies")
        if matches:
            return matches[0]
    return None


def _reject_overlaps(rule_sets: list[RuleSet]) -> None:
    ordered = sorted(rule_sets, key=lambda rs: rs.effective_from)
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        if earlier.effective_to is None or later.effective_from < earlier.effective_to:
            raise RuleSetError(
                f"{earlier.rule_version} and {later.rule_version} overlap "
                f"({earlier.approval_status})"
            )

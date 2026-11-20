"""개선 방안: 점수 부족 원인과 올릴 수 있는 방법(명세 1.2, 07-improvements.md).

- 추천도 규칙 엔진이 계산한다. 증가량과 날짜는 규칙 데이터에서 나오고 LLM은 설명만 한다.
- 증가량은 한 가지만 바꿨을 때 기준이다. 여러 개를 같이 바꾼 결과는 가정 시나리오 재계산으로 본다.
- 바꿀 수 없는 것(나이, 지난 감점 이력)은 추천하지 않는다.
- 누락 입력(답하면 판정되는 것)은 missingInputs에 있으므로 여기서 다루지 않는다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Any

from pydantic import Field

from app.visa.e74.dates import add_months
from app.visa.e74.employment import (
    clip_interval,
    covered_days,
    current_stint,
    intervals_of,
    required_days,
    stint_coverage,
)
from app.visa.e74.results import CamelResponse, ItemResult, ItemStatus
from app.visa.e74.rules import (
    Contract,
    CurrentEmployment,
    DriverLicense,
    EmployerRecommendation,
    EvidencePresent,
    IncomeRange,
    KoreanLevel,
    MinAverageIncome,
    MinKorean,
    Qualification,
    RegionWork,
    Rule,
    RuleActor,
    RuleCategory,
    RuleSet,
    StayPeriod,
    Tenure,
    TotalScore,
)
from app.visa.e74.schemas import E74Profile, EmploymentHistoryIn, EvidenceType
from app.visa.e74.texts import (
    Text,
    english_documents,
    improvement_text,
    rule_text,
    term_text,
    terms_text,
    visa_types_text,
)

SCORED = (
    RuleCategory.BASE_INCOME,
    RuleCategory.BASE_KOREAN,
    RuleCategory.BASE_AGE,
    RuleCategory.BONUS,
)


class ImprovementKind(StrEnum):
    """개선 방안 종류: 요건 해결, 증빙 제출, 기간 대기, 등급 올리기, 가점 얻기."""

    FIX_REQUIREMENT = "FIX_REQUIREMENT"
    SUBMIT_EVIDENCE = "SUBMIT_EVIDENCE"
    WAIT_UNTIL = "WAIT_UNTIL"
    RAISE_LEVEL = "RAISE_LEVEL"
    OBTAIN_BONUS = "OBTAIN_BONUS"


class Improvement(CamelResponse):
    """점수를 올리거나 요건을 채우는 방법 하나(누가, 몇 점, 어떤 서류)."""

    rule_code: str
    kind: ImprovementKind
    actor: RuleActor = Field(
        description="하는 사람: 본인(USER), 고용주(EMPLOYER), 정부 기관(GOVERNMENT)"
    )
    points_gain: int | None = Field(description="확정 총점이 오르는 양. 요건 해결은 null")
    blocking: bool = Field(description="이게 해결되지 않으면 점수와 상관없이 신청할 수 없다")
    available_from: date | None = None
    target: dict[str, Any] = Field(default_factory=dict, description="목표 값과 현재 값")
    documents: tuple[str, ...] = ()
    documents_en: tuple[str, ...] | None = Field(
        default=None,
        description="documents와 같은 순서의 영어 서류 이름(#108). 엔진은 늘 채운다. "
        "이 필드가 생기기 전에 저장된 결과는 null",
    )
    message_code: str
    description: str = Field(
        description="한국어 설명(로드맵 화면 등). LLM에는 descriptionEn을 준다"
    )
    description_en: str | None = Field(
        default=None,
        description="영어 설명(#108). 엔진은 늘 채운다. 이 필드가 생기기 전에 저장된 결과는 null",
    )


class ScoreGap(CamelResponse):
    """기준 총점까지 부족한 점수."""

    required: int | None
    points_needed: int | None = Field(description="기준 − 확정 총점(0 이상)")
    points_needed_after_evidence: int | None = Field(description="기준 − 가능 총점(0 이상)")


class ImprovementPlan(CamelResponse):
    """기준 총점까지 우선순위대로 고른 방안. 로드맵이 단계로 만든다."""

    rule_codes: tuple[str, ...]
    points_added: int
    reaches_required: bool


@dataclass(frozen=True)
class ImprovementAnalysis:
    """개선 방안 분석 결과: 부족 점수, 방안 목록, 기준 총점까지의 계획."""

    score_gap: ScoreGap
    improvements: tuple[Improvement, ...]
    plan: ImprovementPlan


EMPTY_ANALYSIS = ImprovementAnalysis(
    score_gap=ScoreGap(required=None, points_needed=None, points_needed_after_evidence=None),
    improvements=(),
    plan=ImprovementPlan(rule_codes=(), points_added=0, reaches_required=False),
)


# --- 날짜 계산 -------------------------------------------------------------------------


def first_day_reaching(
    rows: Sequence[EmploymentHistoryIn],
    as_of: date,
    months: int,
    *,
    lookback_months: int | None = None,
) -> date | None:
    """현재 근무가 계속된다고 보고, 근무 합계가 months개월을 처음 채우는 날."""
    for offset in range(months * 31 + 1):
        day = as_of + timedelta(days=offset)
        intervals = intervals_of(rows, day)
        if lookback_months is not None:
            window_start = add_months(day, -lookback_months)
            intervals = [c for i in intervals if (c := clip_interval(i, window_start)) is not None]
        if covered_days(intervals) >= required_days(day, months):
            return day
    return None


# --- 요건 ----------------------------------------------------------------------------


def _wording(text: Text, documents: tuple[str, ...]) -> dict[str, Any]:
    """설명·서류의 한국어와 영어(#108). 문구는 texts.IMPROVEMENTS 한곳에 있다."""
    return {
        "description": text.ko,
        "description_en": text.en,
        "documents": documents,
        "documents_en": english_documents(documents),
    }


def _won(amount: int) -> str:
    """금액 표기(천 단위 쉼표). 한국어·영어 문구에 같은 값을 넣는다."""
    return f"{amount:,}"


def _fix(
    rule: Rule,
    message_code: str,
    text: Text,
    *,
    target: dict[str, Any] | None = None,
    available_from: date | None = None,
) -> Improvement:
    return Improvement(
        rule_code=rule.rule_code,
        kind=ImprovementKind.FIX_REQUIREMENT,
        actor=rule.actor,
        points_gain=None,
        blocking=True,
        available_from=available_from,
        target=target or {},
        message_code=message_code,
        **_wording(text, rule.documents),
    )


def _fix_requirement(
    rule: Rule,
    item: ItemResult,
    profile: E74Profile,
    rule_set: RuleSet,
    as_of: date,
    confirmed_total: int,
    required: int | None,
) -> Improvement | None:
    c = rule.condition
    ev = item.evidence
    if isinstance(c, StayPeriod):
        start = profile.visa_profile.current_stay_started_on
        if start is None:
            return None
        day = add_months(start, c.min_months - (profile.visa_profile.prior_stay_months or 0))
        return _fix(
            rule,
            "E74_IMPROVE_STAY_WAIT",
            improvement_text(
                "E74_IMPROVE_STAY_WAIT", years=c.min_months // 12, date=day.isoformat()
            ),
            target={
                "requiredMonths": c.min_months,
                "currentMonths": ev.get("totalMonths", ev.get("currentStayMonths")),
            },
            available_from=day,
        )
    if isinstance(c, CurrentEmployment):
        return _fix(
            rule,
            "E74_IMPROVE_CURRENT_EMPLOYMENT",
            improvement_text(
                "E74_IMPROVE_CURRENT_EMPLOYMENT", visa_types=visa_types_text(c.visa_types)
            ),
            target={"visaTypes": list(c.visa_types)},
        )
    if isinstance(c, Contract):
        salary, required_salary = ev.get("annualSalary"), ev.get("requiredAnnualSalary")
        if (
            isinstance(salary, int)
            and isinstance(required_salary, int)
            and salary < required_salary
        ):
            return _fix(
                rule,
                "E74_IMPROVE_CONTRACT_SALARY",
                improvement_text(
                    "E74_IMPROVE_CONTRACT_SALARY",
                    required=_won(required_salary),
                    salary=_won(salary),
                    shortfall=_won(required_salary - salary),
                ),
                target={
                    "requiredAnnualSalary": required_salary,
                    "annualSalary": salary,
                    "shortfall": required_salary - salary,
                },
            )
        return _fix(
            rule,
            "E74_IMPROVE_CONTRACT_PERIOD",
            improvement_text(
                "E74_IMPROVE_CONTRACT_PERIOD",
                required_end=ev.get("requiredEndOn"),
                contract_end=ev.get("contractEndOn"),
            ),
            target={
                "requiredEndOn": ev.get("requiredEndOn"),
                "contractEndOn": ev.get("contractEndOn"),
            },
        )
    if isinstance(c, EmployerRecommendation):
        stint = current_stint(profile)
        if stint is None:
            return _fix(
                rule,
                "E74_IMPROVE_NEEDS_CURRENT_JOB",
                improvement_text("E74_IMPROVE_NEEDS_CURRENT_JOB"),
            )
        tenure_rows = stint_coverage(stint, as_of, c.min_tenure_months).rows
        tenure_day = first_day_reaching(tenure_rows, as_of, c.min_tenure_months)
        return _fix(
            rule,
            "E74_IMPROVE_EMPLOYER_TENURE_WAIT",
            improvement_text(
                "E74_IMPROVE_EMPLOYER_TENURE_WAIT",
                months=c.min_tenure_months,
                date=tenure_day.isoformat(),
            )
            if tenure_day
            else improvement_text(
                "E74_IMPROVE_EMPLOYER_TENURE_WAIT.later", months=c.min_tenure_months
            ),
            target={"requiredMonths": c.min_tenure_months},
            available_from=tenure_day,
        )
    if isinstance(c, MinAverageIncome):
        average = ev.get("averageIncome")
        if not isinstance(average, int):
            return None
        return _fix(
            rule,
            "E74_IMPROVE_MIN_INCOME",
            improvement_text(
                "E74_IMPROVE_MIN_INCOME",
                required=_won(c.min_amount),
                average=_won(average),
                shortfall=_won(c.min_amount - average),
            ),
            target={
                "requiredAverageIncome": c.min_amount,
                "averageIncome": average,
                "shortfall": c.min_amount - average,
            },
        )
    if isinstance(c, MinKorean):
        lowest = min(_korean_levels(rule_set), key=lambda pair: pair[0].points or 0, default=None)
        if lowest is None:
            return None
        level = lowest[1]
        korean_level = {
            "topik": level.topik_level_min,
            "kiip": level.kiip_stage_min,
            "pre_test": level.pre_test_score_min,
            "stage": level.pre_test_min_assigned_stage,
        }
        if c.deferral_until is not None and as_of <= c.deferral_until:
            text = improvement_text(
                "E74_IMPROVE_MIN_KOREAN.deferral",
                **korean_level,
                until=c.deferral_until.isoformat(),
                min_total=c.deferral_min_total_without_korean,
            )
        else:
            text = improvement_text("E74_IMPROVE_MIN_KOREAN", **korean_level)
        return _fix(
            rule,
            "E74_IMPROVE_MIN_KOREAN",
            text,
            target={
                "topikLevel": level.topik_level_min,
                "kiipStage": level.kiip_stage_min,
                "preTestScore": level.pre_test_score_min,
            },
        )
    if isinstance(c, TotalScore) and required is not None:
        needed = required - confirmed_total
        return _fix(
            rule,
            "E74_IMPROVE_TOTAL_SCORE",
            improvement_text("E74_IMPROVE_TOTAL_SCORE", needed=needed, required=required),
            target={
                "requiredTotal": required,
                "confirmedTotal": confirmed_total,
                "pointsNeeded": needed,
            },
        )
    return None


# --- 점수 항목 ---------------------------------------------------------------------------


def _korean_levels(rule_set: RuleSet) -> list[tuple[Rule, KoreanLevel]]:
    return [
        (r, r.condition)
        for r in rule_set.rules_in(RuleCategory.BASE_KOREAN)
        if isinstance(r.condition, KoreanLevel)
    ]


def _raise_income(item: ItemResult, rule_set: RuleSet, industry: str | None) -> Improvement | None:
    average = item.evidence.get("averageIncome")
    if not isinstance(average, int):
        return None
    current = item.potential_points or 0
    bands = sorted(
        (
            (r, r.condition)
            for r in rule_set.rules_in(RuleCategory.BASE_INCOME)
            if isinstance(r.condition, IncomeRange)
            and (r.condition.industry_codes is None or industry in r.condition.industry_codes)
        ),
        key=lambda pair: pair[1].min_amount,
    )
    nxt = next(
        ((r, c) for r, c in bands if (r.points or 0) > current and c.min_amount > average), None
    )
    if nxt is None:
        return None
    rule, band = nxt
    gain = (rule.points or 0) - current
    return Improvement(
        rule_code=rule.rule_code,
        kind=ImprovementKind.RAISE_LEVEL,
        actor=rule.actor,
        points_gain=gain,
        blocking=False,
        target={
            "averageIncomeNeeded": band.min_amount,
            "currentAverage": average,
            "shortfall": band.min_amount - average,
        },
        message_code="E74_IMPROVE_RAISE_INCOME",
        **_wording(
            improvement_text(
                "E74_IMPROVE_RAISE_INCOME",
                needed=_won(band.min_amount),
                gain=gain,
                average=_won(average),
            ),
            rule.documents,
        ),
    )


def _raise_korean(item: ItemResult, rule_set: RuleSet) -> Improvement | None:
    if item.potential_points is None and item.status is not ItemStatus.NOT_MET:
        return None  # 한국어 정보가 없으면 재질의가 먼저다
    current = item.potential_points or 0
    levels = sorted(_korean_levels(rule_set), key=lambda pair: pair[0].points or 0)
    nxt = next(((r, c) for r, c in levels if (r.points or 0) > current), None)
    if nxt is None:
        return None
    rule, level = nxt
    gain = (rule.points or 0) - current
    return Improvement(
        rule_code=rule.rule_code,
        kind=ImprovementKind.RAISE_LEVEL,
        actor=rule.actor,
        points_gain=gain,
        blocking=False,
        target={
            "topikLevel": level.topik_level_min,
            "kiipStage": level.kiip_stage_min,
            "preTestScore": level.pre_test_score_min,
        },
        message_code="E74_IMPROVE_RAISE_KOREAN",
        **_wording(
            improvement_text(
                "E74_IMPROVE_RAISE_KOREAN",
                topik=level.topik_level_min,
                kiip=level.kiip_stage_min,
                pre_test=level.pre_test_score_min,
                gain=gain,
            ),
            rule.documents,
        ),
    )


def _blocks(rule: Rule, unmet_evidence: frozenset[EvidenceType]) -> bool:
    """필수요건이기도 한 가점(고용기업 추천)은 그 요건이 충족되지 않았으면 신청을 막는다."""
    c = rule.condition
    return isinstance(c, EvidencePresent) and c.evidence_type in unmet_evidence


def _bonus_improvement(
    rule: Rule,
    items: dict[str, ItemResult],
    rule_set: RuleSet,
    profile: E74Profile,
    as_of: date,
    seen_groups: set[str],
    unmet_evidence: frozenset[EvidenceType],
) -> Improvement | None:
    c = rule.condition
    points = rule.points or 0
    blocking = _blocks(rule, unmet_evidence)

    def make(kind: ImprovementKind, code: str, text: Text, **extra: Any) -> Improvement:
        return Improvement(
            rule_code=rule.rule_code,
            kind=kind,
            actor=rule.actor,
            points_gain=points,
            blocking=blocking,
            message_code=code,
            **_wording(text, rule.documents),
            **extra,
        )

    if isinstance(c, EvidencePresent):
        group = rule.exclusive_group
        if group is None:
            return make(
                ImprovementKind.OBTAIN_BONUS,
                "E74_IMPROVE_OBTAIN_RECOMMENDATION",
                improvement_text(
                    "E74_IMPROVE_OBTAIN_RECOMMENDATION",
                    rule=rule_text(rule.rule_code, rule.description),
                    points=points,
                ),
            )
        if group in seen_groups:
            return None
        seen_groups.add(group)
        members = [r for r in rule_set.rules if r.exclusive_group == group]
        if any(
            (items[m.rule_code].potential_points or 0) > 0 for m in members if m.rule_code in items
        ):
            return None
        return make(
            ImprovementKind.OBTAIN_BONUS,
            "E74_IMPROVE_OBTAIN_RECOMMENDATION",
            improvement_text("E74_IMPROVE_OBTAIN_RECOMMENDATION.group", points=points),
            target={"alternatives": [m.rule_code for m in members]},
        )
    if isinstance(c, Tenure):
        stint = current_stint(profile)
        if stint is None:
            return None
        rows = stint_coverage(stint, as_of, c.min_months).rows
        day = first_day_reaching(rows, as_of, c.min_months)
        if day is None:
            return None
        return make(
            ImprovementKind.WAIT_UNTIL,
            "E74_IMPROVE_TENURE_WAIT",
            improvement_text("E74_IMPROVE_TENURE_WAIT", date=day.isoformat(), points=points),
            available_from=day,
        )
    if isinstance(c, RegionWork):
        current = profile.current_employment
        if current is None or current.is_depopulation_area is not True:
            return None  # 이사·이직을 권하지 않는다
        region_rows = [r for r in profile.employment_histories if r.is_depopulation_area is True]
        region_day = first_day_reaching(
            region_rows, as_of, c.min_months, lookback_months=c.lookback_months
        )
        if region_day is None:
            return None
        return make(
            ImprovementKind.WAIT_UNTIL,
            "E74_IMPROVE_REGION_WAIT",
            improvement_text("E74_IMPROVE_REGION_WAIT", date=region_day.isoformat(), points=points),
            available_from=region_day,
        )
    if isinstance(c, Qualification):
        return make(
            ImprovementKind.OBTAIN_BONUS,
            "E74_IMPROVE_OBTAIN_QUALIFICATION",
            improvement_text(
                "E74_IMPROVE_OBTAIN_QUALIFICATION",
                grades=terms_text(c.certificate_grades),
                degree=term_text(c.degree_levels[0]),
                points=points,
            ),
        )
    if isinstance(c, DriverLicense):
        return make(
            ImprovementKind.OBTAIN_BONUS,
            "E74_IMPROVE_OBTAIN_DRIVER_LICENSE",
            improvement_text("E74_IMPROVE_OBTAIN_DRIVER_LICENSE", points=points),
        )
    return None


# --- 정렬과 계획 ---------------------------------------------------------------------------

_KIND_ORDER = {
    ImprovementKind.FIX_REQUIREMENT: 0,
    ImprovementKind.SUBMIT_EVIDENCE: 1,
    ImprovementKind.WAIT_UNTIL: 2,
    ImprovementKind.RAISE_LEVEL: 3,
    ImprovementKind.OBTAIN_BONUS: 3,
}
_ACTOR_ORDER = {RuleActor.USER: 0, RuleActor.EMPLOYER: 1, RuleActor.GOVERNMENT: 2}


def _sorted(improvements: list[Improvement], rule_set: RuleSet) -> tuple[Improvement, ...]:
    order = {r.rule_code: i for i, r in enumerate(rule_set.rules)}
    return tuple(
        sorted(
            improvements,
            key=lambda imp: (
                _KIND_ORDER[imp.kind],
                not imp.blocking,
                imp.available_from or date.max,
                -(imp.points_gain or 0),
                _ACTOR_ORDER[imp.actor],
                order.get(imp.rule_code, len(order)),
            ),
        )
    )


def _plan(improvements: Sequence[Improvement], needed: int | None) -> ImprovementPlan:
    if needed is None:
        return ImprovementPlan(rule_codes=(), points_added=0, reaches_required=False)
    if needed <= 0:
        return ImprovementPlan(rule_codes=(), points_added=0, reaches_required=True)
    chosen: list[str] = []
    added = 0
    for imp in improvements:
        if not imp.points_gain or imp.points_gain <= 0:
            continue
        chosen.append(imp.rule_code)
        added += imp.points_gain
        if added >= needed:
            break
    return ImprovementPlan(
        rule_codes=tuple(chosen), points_added=added, reaches_required=added >= needed
    )


# --- 진입점 ----------------------------------------------------------------------------


def analyze_improvements(
    profile: E74Profile,
    rule_set: RuleSet,
    as_of: date,
    items: Sequence[ItemResult],
    *,
    required: int | None,
    confirmed_total: int,
    potential_total: int,
) -> ImprovementAnalysis:
    rules = {r.rule_code: r for r in rule_set.rules}
    by_code = {item.rule_code: item for item in items if item.rule_code is not None}
    derived = (MinAverageIncome, MinKorean, TotalScore)
    current = profile.current_employment
    industry = current.industry_code if current is not None else None
    found: list[Improvement] = []
    seen_groups: set[str] = set()
    # 아직 충족되지 않은(NEEDS_EVIDENCE·NOT_MET) 추천 요건이 요구하는 증빙 종류.
    unmet_evidence = frozenset(
        rule.condition.evidence_type
        for item in items
        if item.category is RuleCategory.REQUIREMENT
        and item.status in (ItemStatus.NEEDS_EVIDENCE, ItemStatus.NOT_MET)
        and (rule := rules.get(item.rule_code or "")) is not None
        and isinstance(rule.condition, EmployerRecommendation)
    )

    for item in items:
        rule = rules.get(item.rule_code or "")
        if rule is None:
            continue
        if item.category is RuleCategory.REQUIREMENT:
            if item.status is ItemStatus.NOT_MET:
                fix = _fix_requirement(
                    rule, item, profile, rule_set, as_of, confirmed_total, required
                )
                if fix is not None:
                    found.append(fix)
            elif (
                item.status is ItemStatus.NEEDS_EVIDENCE
                and not item.missing_inputs
                and not isinstance(rule.condition, derived)
            ):
                found.append(
                    Improvement(
                        rule_code=rule.rule_code,
                        kind=ImprovementKind.SUBMIT_EVIDENCE,
                        actor=rule.actor,
                        points_gain=None,
                        blocking=True,
                        message_code="E74_IMPROVE_SUBMIT_REQUIREMENT_EVIDENCE",
                        **_wording(
                            improvement_text(
                                "E74_IMPROVE_SUBMIT_REQUIREMENT_EVIDENCE",
                                rule=rule_text(rule.rule_code, rule.description),
                            ),
                            rule.documents,
                        ),
                    )
                )
        elif item.category in SCORED:
            earned, potential = item.earned_points or 0, item.potential_points or 0
            if (
                item.status is ItemStatus.NEEDS_EVIDENCE
                and not item.missing_inputs
                and potential > earned
            ):
                found.append(
                    Improvement(
                        rule_code=rule.rule_code,
                        kind=ImprovementKind.SUBMIT_EVIDENCE,
                        actor=rule.actor,
                        points_gain=potential - earned,
                        blocking=_blocks(rule, unmet_evidence),
                        message_code="E74_IMPROVE_SUBMIT_EVIDENCE",
                        **_wording(
                            improvement_text(
                                "E74_IMPROVE_SUBMIT_EVIDENCE",
                                rule=rule_text(rule.rule_code, rule.description),
                                points=potential - earned,
                            ),
                            rule.documents,
                        ),
                    )
                )
            if item.category is RuleCategory.BONUS and item.status is ItemStatus.NOT_MET:
                bonus = _bonus_improvement(
                    rule, by_code, rule_set, profile, as_of, seen_groups, unmet_evidence
                )
                if bonus is not None:
                    found.append(bonus)

    income = next((i for i in items if i.category is RuleCategory.BASE_INCOME), None)
    korean = next((i for i in items if i.category is RuleCategory.BASE_KOREAN), None)
    for raised in (
        _raise_income(income, rule_set, industry) if income is not None else None,
        _raise_korean(korean, rule_set) if korean is not None else None,
    ):
        if raised is not None:
            found.append(raised)

    improvements = _sorted(found, rule_set)
    needed = None if required is None else max(0, required - confirmed_total)
    gap = ScoreGap(
        required=required,
        points_needed=needed,
        points_needed_after_evidence=None
        if required is None
        else max(0, required - potential_total),
    )
    return ImprovementAnalysis(
        score_gap=gap, improvements=improvements, plan=_plan(improvements, needed)
    )

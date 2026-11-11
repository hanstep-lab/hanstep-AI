"""개선 방안 → 로드맵 단계: 신청 가능일, 단계 고르기, 단계를 했다고 가정한 시나리오.

- 단계는 엔진이 추천한 개선 방안에서만 고른다(나이·지난 감점은 엔진이 추천하지 않는다).
- 기다리면 채워지는 것(체류기간, 근속)은 단계가 아니라 신청 가능일·단계 날짜로만 낸다.
- 예상 점수는 단계 점수를 더하지 않고, 가정을 반영한 프로필로 엔진을 다시 돌려 얻는다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from pydantic.alias_generators import to_camel

from app.visa.e74.engine import E74Assessment
from app.visa.e74.improvements import Improvement, ImprovementKind
from app.visa.e74.results import ItemResult, ItemStatus
from app.visa.e74.rules import (
    DriverLicense,
    EvidencePresent,
    IncomeRange,
    KoreanLevel,
    MinAverageIncome,
    MinKorean,
    Qualification,
    RuleCategory,
    RuleSet,
    StayPeriod,
    TotalScore,
)
from app.visa.e74.schemas import (
    E74Profile,
    E74Scenario,
    EvidenceType,
    ScenarioEvidenceIn,
    ScenarioIncomeIn,
)

MAX_ACTION_STEPS = 3


def _evidence(evidence_type: EvidenceType, **fields: object) -> ScenarioEvidenceIn:
    """요청 모델은 camelCase 이름으로만 받으므로 별칭으로 만든다."""
    body = {"evidenceType": evidence_type, **{to_camel(k): v for k, v in fields.items()}}
    return ScenarioEvidenceIn.model_validate(body)


def stay_item(assessment: E74Assessment, rule_set: RuleSet) -> ItemResult | None:
    codes = {r.rule_code for r in rule_set.rules if isinstance(r.condition, StayPeriod)}
    return next((i for i in assessment.items if i.rule_code in codes), None)


def applicable_from(assessment: E74Assessment, rule_set: RuleSet) -> date | None:
    """신청 가능일. 체류기간을 판정할 수 없으면(재질의·검토 필요) None.

    체류기간을 채웠으면 기준일, 아직이면 엔진이 계산한 충족일. 다른 요건에도 기다리는 날짜
    (예: 고용기업 추천을 받을 근속)가 있으면 그중 가장 늦은 날.
    """
    stay = stay_item(assessment, rule_set)
    if stay is None:
        return None
    if stay.status is ItemStatus.MET:
        day = assessment.as_of_date
    elif stay.status is ItemStatus.NOT_MET:
        fix = next(
            (
                imp
                for imp in assessment.improvements
                if imp.rule_code == stay.rule_code and imp.available_from is not None
            ),
            None,
        )
        if fix is None or fix.available_from is None:
            return None
        day = fix.available_from
    else:
        return None
    waits = [
        imp.available_from
        for imp in assessment.improvements
        if imp.kind is ImprovementKind.FIX_REQUIREMENT and imp.available_from is not None
    ]
    return max([day, *waits])


def _is_step(imp: Improvement, rule_set: RuleSet) -> bool:
    if imp.kind is ImprovementKind.WAIT_UNTIL:
        return False
    if imp.kind is ImprovementKind.FIX_REQUIREMENT:
        condition = rule_set.rule(imp.rule_code).condition
        # 체류기간은 신청 가능일로만, 총점 부족은 아래 단계들로 채운다(기준 점수 노출 방지).
        # 날짜가 있는 요건은 기다리면 충족되므로 신청 가능일에 반영한다.
        if isinstance(condition, (StayPeriod, TotalScore)) or imp.available_from is not None:
            return False
    return True


def _group(imp: Improvement, rule_set: RuleSet) -> str:
    """같은 묶음(예: 중앙부처·광역지자체 추천)은 하나만 고른다."""
    group = rule_set.rule(imp.rule_code).exclusive_group
    if group is not None:
        return group
    alternatives = imp.target.get("alternatives")
    if isinstance(alternatives, list) and alternatives:
        return "|".join(sorted(str(a) for a in alternatives))
    return imp.rule_code


def select_steps(assessment: E74Assessment, rule_set: RuleSet) -> list[Improvement]:
    """최대 3개. 필수(blocking) → 증빙 제출 → 기준 총점 계획 순서 → 점수 큰 순."""
    plan = {code: i for i, code in enumerate(assessment.plan_to_required.rule_codes)}
    engine_order = {imp.rule_code: i for i, imp in enumerate(assessment.improvements)}
    ranked = sorted(
        (imp for imp in assessment.improvements if _is_step(imp, rule_set)),
        key=lambda imp: (
            not imp.blocking,
            imp.kind is not ImprovementKind.SUBMIT_EVIDENCE,
            plan.get(imp.rule_code, len(plan)),
            -(imp.points_gain or 0),
            engine_order[imp.rule_code],
        ),
    )
    chosen: list[Improvement] = []
    groups: set[str] = set()
    for imp in ranked:
        group = _group(imp, rule_set)
        if group in groups:
            continue
        groups.add(group)
        chosen.append(imp)
        if len(chosen) == MAX_ACTION_STEPS:
            break
    return chosen


# --- 가정 시나리오 ------------------------------------------------------------------------


@dataclass
class Assumptions:
    evidences: list[ScenarioEvidenceIn] = field(default_factory=list)
    incomes: dict[int, int] = field(default_factory=dict)

    def add_incomes(self, years: Sequence[int], amount: int) -> None:
        for year in years:
            self.incomes[year] = max(amount, self.incomes.get(year, 0))


def _income_years(assessment: E74Assessment) -> list[int] | None:
    income = next((i for i in assessment.items if i.category is RuleCategory.BASE_INCOME), None)
    years = income.evidence.get("incomeYears") if income is not None else None
    if not isinstance(years, list) or not all(isinstance(y, int) for y in years):
        return None
    return years


def _resubmit(item: ItemResult | None, profile: E74Profile) -> list[ScenarioEvidenceIn] | None:
    """신고만 된 증빙을 서류로 확인받았다고 가정한다(같은 내용의 VERIFIED 증빙 추가)."""
    if item is None:
        return None
    ids = item.evidence.get("visaEvidenceIds") or [item.evidence.get("visaEvidenceId")]
    rows = [e for e in profile.visa_evidences if e.visa_evidence_id in ids]
    if not rows:
        return None
    return [
        _evidence(
            e.evidence_type,
            level_value=e.level_value,
            pre_test_score=e.pre_test_score,
            pre_test_assigned_stage=e.pre_test_assigned_stage,
            related_to_current_job=e.related_to_current_job,
        )
        for e in rows
    ]


def _topik(level: object) -> ScenarioEvidenceIn | None:
    if not isinstance(level, int):
        return None
    return _evidence(EvidenceType.TOPIK, level_value=str(level))


def add_assumption(
    into: Assumptions,
    imp: Improvement,
    assessment: E74Assessment,
    rule_set: RuleSet,
    profile: E74Profile,
) -> bool:
    """단계를 했다고 가정한 입력을 더한다. 시나리오로 나타낼 수 없으면 False."""
    rule = rule_set.rule(imp.rule_code)
    c = rule.condition
    item = next((i for i in assessment.items if i.rule_code == imp.rule_code), None)
    years = _income_years(assessment)

    if imp.kind is ImprovementKind.SUBMIT_EVIDENCE:
        if isinstance(c, IncomeRange):
            average = item.evidence.get("averageIncome") if item is not None else None
            if years is None or not isinstance(average, int):
                return False
            into.add_incomes(years, average)
            return True
        if isinstance(c, EvidencePresent):
            into.evidences.append(_evidence(c.evidence_type))
            return True
        resubmitted = _resubmit(item, profile)
        if resubmitted is None:
            return False
        into.evidences.extend(resubmitted)
        return True

    if isinstance(c, EvidencePresent):
        into.evidences.append(_evidence(c.evidence_type))
        return True
    if isinstance(c, (KoreanLevel, MinKorean)):
        topik = _topik(imp.target.get("topikLevel"))
        if topik is None:
            return False
        into.evidences.append(topik)
        return True
    if isinstance(c, IncomeRange):
        needed = imp.target.get("averageIncomeNeeded")
        if years is None or not isinstance(needed, int):
            return False
        into.add_incomes(years, needed)
        return True
    if isinstance(c, MinAverageIncome):
        if years is None:
            return False
        into.add_incomes(years, c.min_amount)
        return True
    if isinstance(c, Qualification):
        into.evidences.append(
            _evidence(
                EvidenceType.NATIONAL_CERTIFICATE,
                level_value=c.certificate_grades[-1],
                related_to_current_job=True,
            )
        )
        return True
    if isinstance(c, DriverLicense):
        into.evidences.append(
            _evidence(EvidenceType.DRIVER_LICENSE, level_value=c.accepted_classes[-1])
        )
        return True
    # 고용계약·근무처 같은 요건은 시나리오로 나타낼 수 없다.
    return False


def scenario_for(
    steps: Sequence[Improvement],
    assessment: E74Assessment,
    rule_set: RuleSet,
    profile: E74Profile,
) -> E74Scenario | None:
    """단계들을 모두 했다고 가정한 시나리오. 나타낼 수 없는 단계가 있으면 None."""
    assumptions = Assumptions()
    for imp in steps:
        if not add_assumption(assumptions, imp, assessment, rule_set, profile):
            return None
    return E74Scenario.model_validate(
        {
            "addEvidences": assumptions.evidences,
            "incomeRecords": [
                ScenarioIncomeIn.model_validate({"incomeYear": year, "annualAmount": amount})
                for year, amount in sorted(assumptions.incomes.items())
            ],
        }
    )

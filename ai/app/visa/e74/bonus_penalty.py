"""가점·감점 (명세 3.3, 정의서 3·4장).

- 규칙 하나당 항목 결과 하나(MySQL uq_e74_assessment_items(assessment_id, rule_code)).
- 가점 증빙이 아예 없으면 NOT_MET 0점. 판정에 꼭 필요한 값이 비면 재질의.
- 확정 점수는 기여한 행이 모두 VERIFIED일 때만.
- 감점은 자기신고 값으로 계산하고, 신고가 없으면 0점으로 확정하지 않는다.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from typing import Any

from app.visa.e74.dates import add_months
from app.visa.e74.employment import (
    Interval,
    clip_interval,
    covered_days,
    current_stint,
    employment_interval,
    intervals_of,
    required_days,
)
from app.visa.e74.questions import declaration_missing, declared_value
from app.visa.e74.results import (
    ItemResult,
    ItemStatus,
    MissingInput,
    missing_result,
    not_met_result,
    scored_result,
)
from app.visa.e74.rules import (
    DeclaredCountAtLeast,
    DeclaredCountPenalty,
    DriverLicense,
    EvidencePresent,
    Qualification,
    RegionWork,
    Rule,
    RuleCategory,
    RuleSet,
    Tenure,
)
from app.visa.e74.schemas import (
    E74Profile,
    EmploymentHistoryIn,
    EvidenceType,
    VerificationStatus,
    VisaEvidenceIn,
)

BONUS = RuleCategory.BONUS
PENALTY = RuleCategory.PENALTY
# 선발계획 인정 목록(기사·산업기사·기능사)에 없는 상위 등급. 인정 여부는 사람이 확인한다.
HIGHER_CERTIFICATE_GRADES = ("기술사", "기능장")
_LICENSE_NOISE = re.compile(r"\s|제|면허")

EMPLOYMENT_MISSING = MissingInput(
    field="employmentHistories", question="근무 이력(사업장, 업종, 근무 시작일·종료일)"
)


def _first_rule(rule_set: RuleSet, category: RuleCategory, kind: type) -> Rule | None:
    return next((r for r in rule_set.rules_in(category) if isinstance(r.condition, kind)), None)


def _usable_evidences(profile: E74Profile, evidence_type: EvidenceType) -> list[VisaEvidenceIn]:
    return [
        ev
        for ev in profile.visa_evidences
        if ev.evidence_type is evidence_type and ev.status is not VerificationStatus.REJECTED
    ]


def _all_verified(rows: list[EmploymentHistoryIn]) -> bool:
    return bool(rows) and all(r.verification_status is VerificationStatus.VERIFIED for r in rows)


def _ids(rows: list[EmploymentHistoryIn]) -> list[int]:
    return sorted(r.employment_history_id for r in rows)


# --- 추천 ------------------------------------------------------------------------


def _recommendation_item(
    profile: E74Profile, rule: Rule, evidence_type: EvidenceType
) -> ItemResult:
    found = _usable_evidences(profile, evidence_type)
    evidence: dict[str, Any] = {"visaEvidenceIds": [e.visa_evidence_id for e in found]}
    if not found:
        return not_met_result(BONUS, "E74_BONUS_NOT_FOUND", evidence, rule_code=rule.rule_code)
    verified = any(e.status is VerificationStatus.VERIFIED for e in found)
    return scored_result(
        BONUS,
        rule,
        rule if verified else None,
        message_code="E74_BONUS_RECOMMENDATION",
        evidence=evidence,
    )


def calculate_recommendation_bonus(profile: E74Profile, rule_set: RuleSet) -> list[ItemResult]:
    """중앙부처·광역지자체·고용기업 추천. 같은 중복 그룹은 하나만 센다."""
    items = [
        _recommendation_item(profile, rule, rule.condition.evidence_type)
        for rule in rule_set.rules_in(BONUS)
        if isinstance(rule.condition, EvidencePresent)
    ]
    return apply_exclusive_groups(items, rule_set)


def apply_exclusive_groups(items: list[ItemResult], rule_set: RuleSet) -> list[ItemResult]:
    """같은 exclusiveGroup 항목 중 하나만 점수를 남긴다.

    확정 점수 → 가능 점수 → 규칙 순서로 하나를 고르고, 점수가 있던 나머지는 NOT_APPLICABLE.
    """
    group_of = {r.rule_code: r.exclusive_group for r in rule_set.rules if r.exclusive_group}
    members: dict[str, list[int]] = {}
    for index, item in enumerate(items):
        group = group_of.get(item.rule_code or "")
        if group is not None:
            members.setdefault(group, []).append(index)

    result = list(items)
    for group, indexes in members.items():

        def rank(i: int) -> tuple[int, int, int]:
            return (result[i].earned_points or 0, result[i].potential_points or 0, -i)

        winner = max(indexes, key=rank)
        if not result[winner].potential_points:
            continue
        for i in indexes:
            item = result[i]
            if i == winner or not item.potential_points:
                continue
            result[i] = item.model_copy(
                update={
                    "status": ItemStatus.NOT_APPLICABLE,
                    "earned_points": 0,
                    "potential_points": 0,
                    "message_code": "E74_BONUS_SAME_GROUP",
                    "evidence": {
                        **item.evidence,
                        "exclusiveGroup": group,
                        "countedRuleCode": result[winner].rule_code,
                        "ownStatus": item.status.value,
                    },
                    "missing_inputs": (),
                    "review_note": None,
                }
            )
    return result


# --- 현 근무처 근속 -------------------------------------------------------------------


def calculate_tenure_bonus(
    profile: E74Profile, rule_set: RuleSet, as_of: date
) -> ItemResult | None:
    """현 근무처 근속. 같은 업체 재고용 기간과 귀책 없는 이직 직전 근무처 기간을 합산한다."""
    rule = _first_rule(rule_set, BONUS, Tenure)
    if rule is None or not isinstance(rule.condition, Tenure):
        return None
    code = rule.rule_code
    if not profile.employment_histories:
        return missing_result(BONUS, "E74_EMPLOYMENT_MISSING", [EMPLOYMENT_MISSING], rule_code=code)
    stint = current_stint(profile)
    if stint is None:
        return not_met_result(BONUS, "E74_TENURE_NO_CURRENT_JOB", {}, rule_code=code)

    need = required_days(as_of, rule.condition.min_months)
    rows = list(stint.same_employer)
    previous = stint.previous
    if previous is not None and previous.left_without_fault is True:
        rows.append(previous)
    days = covered_days(intervals_of(rows, as_of))
    evidence: dict[str, Any] = {
        "coveredDays": days,
        "requiredDays": need,
        "employmentHistoryIds": _ids(rows),
    }
    if days >= need:
        return scored_result(
            BONUS,
            rule,
            rule if _all_verified(rows) else None,
            message_code="E74_BONUS_TENURE",
            evidence=evidence,
        )
    if previous is not None and previous.left_without_fault is None:
        with_previous = covered_days(intervals_of([*rows, previous], as_of))
        if with_previous >= need:
            question = (
                "직전 근무처를 휴·폐업, 임금체불, 폭언·폭행 등 본인 잘못이 아닌 이유로 옮겼는지"
            )
            return missing_result(
                BONUS,
                "E74_TENURE_NEEDS_JOB_CHANGE_REASON",
                [
                    MissingInput(
                        field=(
                            f"employmentHistories[employmentHistoryId="
                            f"{previous.employment_history_id}].leftWithoutFault"
                        ),
                        question=question,
                    )
                ],
                {**evidence, "coveredDaysWithPrevious": with_previous},
                rule_code=code,
            )
    return not_met_result(BONUS, "E74_BONUS_TENURE_SHORT", evidence, rule_code=code)


# --- 인구감소지역·읍면 근무 ------------------------------------------------------------


def calculate_region_bonus(
    profile: E74Profile, rule_set: RuleSet, as_of: date
) -> ItemResult | None:
    """최근 lookback 안의 인구감소지역·읍면 근무 합계. 겹친 기간은 한 번만 센다."""
    rule = _first_rule(rule_set, BONUS, RegionWork)
    if rule is None or not isinstance(rule.condition, RegionWork):
        return None
    code = rule.rule_code
    if not profile.employment_histories:
        return missing_result(BONUS, "E74_EMPLOYMENT_MISSING", [EMPLOYMENT_MISSING], rule_code=code)

    window_start = add_months(as_of, -rule.condition.lookback_months)
    need = required_days(as_of, rule.condition.min_months)

    def in_window(rows: list[EmploymentHistoryIn]) -> list[Interval]:
        clipped: list[Interval] = []
        for row in rows:
            interval = employment_interval(row, as_of)
            part = clip_interval(interval, window_start) if interval is not None else None
            if part is not None:
                clipped.append(part)
        return clipped

    rows = [r for r in profile.employment_histories if r.is_depopulation_area is True]
    rows = [r for r in rows if in_window([r])]
    unknown = [
        r for r in profile.employment_histories if r.is_depopulation_area is None and in_window([r])
    ]
    days = covered_days(in_window(rows))
    evidence: dict[str, Any] = {
        "coveredDays": days,
        "requiredDays": need,
        "windowStart": window_start.isoformat(),
        "employmentHistoryIds": _ids(rows),
    }
    if days >= need:
        return scored_result(
            BONUS,
            rule,
            rule if _all_verified(rows) else None,
            message_code="E74_BONUS_REGION",
            evidence=evidence,
        )
    if unknown and covered_days(in_window(rows + unknown)) >= need:
        missing = [
            MissingInput(
                field=(
                    f"employmentHistories[employmentHistoryId={r.employment_history_id}]"
                    ".isDepopulationArea"
                ),
                question=(
                    f"{r.started_on.isoformat()}~{r.ended_on.isoformat() if r.ended_on else '현재'}"
                    " 근무지가 인구감소지역 또는 읍·면 지역인지"
                ),
            )
            for r in unknown
        ]
        return missing_result(BONUS, "E74_REGION_UNKNOWN_AREA", missing, evidence, rule_code=code)
    return not_met_result(BONUS, "E74_BONUS_REGION_SHORT", evidence, rule_code=code)


# --- 자격증·학위, 운전면허 ---------------------------------------------------------------


def _certificate_grade(name: str, grades: tuple[str, ...]) -> str | None:
    # 산업기사도 "기사"로 끝나므로 긴 등급부터 비교한다.
    return next((g for g in sorted(grades, key=len, reverse=True) if name.endswith(g)), None)


def _evidence_path(ev: VisaEvidenceIn, attribute: str) -> str:
    return f"visaEvidences[visaEvidenceId={ev.visa_evidence_id}].{attribute}"


def _recognition_result(
    rule: Rule,
    *,
    recognized: list[VisaEvidenceIn],
    missing: list[MissingInput],
    review_note: str | None,
    considered: list[dict[str, Any]],
    message_code: str,
) -> ItemResult:
    """여러 증빙 중 하나만 인정되면 되는 가점의 결과."""
    evidence: dict[str, Any] = {"candidates": considered}
    verified = [ev for ev in recognized if ev.status is VerificationStatus.VERIFIED]
    if recognized:
        counted = verified[0] if verified else recognized[0]
        evidence["visaEvidenceId"] = counted.visa_evidence_id
        return scored_result(
            BONUS, rule, rule if verified else None, message_code=message_code, evidence=evidence
        )
    if missing:
        return missing_result(BONUS, message_code, missing, evidence, rule_code=rule.rule_code)
    if review_note is not None:
        return scored_result(
            BONUS, rule, None, message_code=message_code, evidence=evidence, review_note=review_note
        )
    return not_met_result(BONUS, "E74_BONUS_NOT_FOUND", evidence, rule_code=rule.rule_code)


def calculate_qualification_bonus(profile: E74Profile, rule_set: RuleSet) -> ItemResult | None:
    """국내 자격증(인정 등급, 근무 분야 관련) 또는 국내 학위. 둘 다 있어도 한 번."""
    rule = _first_rule(rule_set, BONUS, Qualification)
    if rule is None or not isinstance(rule.condition, Qualification):
        return None
    condition = rule.condition
    recognized: list[VisaEvidenceIn] = []
    missing: list[MissingInput] = []
    considered: list[dict[str, Any]] = []
    review_note: str | None = None

    for ev in _usable_evidences(profile, EvidenceType.NATIONAL_CERTIFICATE):
        name = (ev.level_value or "").replace(" ", "")
        detail: dict[str, Any] = {"visaEvidenceId": ev.visa_evidence_id, "name": name or None}
        considered.append(detail)
        if not name:
            missing.append(
                MissingInput(
                    field=_evidence_path(ev, "levelValue"), question="자격증 이름(예: 용접기능사)"
                )
            )
            continue
        if _certificate_grade(name, condition.certificate_grades) is None:
            if name.endswith(HIGHER_CERTIFICATE_GRADES):
                review_note = (
                    "기술사·기능장은 선발계획 인정 목록(기사·산업기사·기능사)에 없어 확인 필요"
                )
            detail["excluded"] = "GRADE_NOT_LISTED"
            continue
        if condition.require_job_related and ev.related_to_current_job is not True:
            if ev.related_to_current_job is None:
                missing.append(
                    MissingInput(
                        field=_evidence_path(ev, "relatedToCurrentJob"),
                        question=f"자격증 '{name}'이 지금 하는 일과 관련 있는지",
                    )
                )
            else:
                detail["excluded"] = "NOT_JOB_RELATED"
            continue
        recognized.append(ev)

    for ev in _usable_evidences(profile, EvidenceType.KOREAN_DEGREE):
        value = (ev.level_value or "").replace(" ", "")
        detail = {"visaEvidenceId": ev.visa_evidence_id, "degree": value or None}
        considered.append(detail)
        if not value:
            missing.append(
                MissingInput(
                    field=_evidence_path(ev, "levelValue"),
                    question="국내 학위 과정(전문학사, 학사, 석사, 박사)",
                )
            )
        elif any(level in value for level in condition.degree_levels):
            recognized.append(ev)
        else:
            detail["excluded"] = "DEGREE_NOT_LISTED"

    return _recognition_result(
        rule,
        recognized=recognized,
        missing=missing,
        review_note=review_note,
        considered=considered,
        message_code="E74_BONUS_QUALIFICATION",
    )


def _license_class(value: str) -> str:
    """ "제2종 보통면허" → "2종보통"."""
    return _LICENSE_NOISE.sub("", value)


def calculate_license_bonus(profile: E74Profile, rule_set: RuleSet) -> ItemResult | None:
    """국내 운전면허 제2종 보통 이상."""
    rule = _first_rule(rule_set, BONUS, DriverLicense)
    if rule is None or not isinstance(rule.condition, DriverLicense):
        return None
    accepted = {_license_class(c) for c in rule.condition.accepted_classes}
    recognized: list[VisaEvidenceIn] = []
    missing: list[MissingInput] = []
    considered: list[dict[str, Any]] = []
    for ev in _usable_evidences(profile, EvidenceType.DRIVER_LICENSE):
        license_class = _license_class(ev.level_value or "")
        detail: dict[str, Any] = {
            "visaEvidenceId": ev.visa_evidence_id,
            "class": license_class or None,
        }
        considered.append(detail)
        if not license_class:
            missing.append(
                MissingInput(
                    field=_evidence_path(ev, "levelValue"), question="운전면허 종류(예: 2종 보통)"
                )
            )
        elif license_class in accepted:
            recognized.append(ev)
        else:
            detail["excluded"] = "CLASS_NOT_ACCEPTED"
    return _recognition_result(
        rule,
        recognized=recognized,
        missing=missing,
        review_note=None,
        considered=considered,
        message_code="E74_BONUS_DRIVER_LICENSE",
    )


def calculate_bonus_items(profile: E74Profile, rule_set: RuleSet, as_of: date) -> list[ItemResult]:
    """가점 항목 전체(규칙 순서). 중복 그룹 제한을 적용한다."""
    items = calculate_recommendation_bonus(profile, rule_set)
    others: list[Callable[[], ItemResult | None]] = [
        lambda: calculate_tenure_bonus(profile, rule_set, as_of),
        lambda: calculate_region_bonus(profile, rule_set, as_of),
        lambda: calculate_qualification_bonus(profile, rule_set),
        lambda: calculate_license_bonus(profile, rule_set),
    ]
    items += [item for make in others if (item := make()) is not None]
    order = {r.rule_code: i for i, r in enumerate(rule_set.rules)}
    items.sort(key=lambda item: order.get(item.rule_code or "", len(order)))
    return apply_exclusive_groups(items, rule_set)


# --- 감점 --------------------------------------------------------------------------


def calculate_penalties(profile: E74Profile, rule_set: RuleSet) -> list[ItemResult]:
    """자기신고 횟수별 감점. 같은 신고 필드의 제외 기준에 걸리면 감점 대신 제외(NOT_APPLICABLE)."""
    exclusion_thresholds = {
        rule.condition.declaration_field: rule.condition.min_count
        for rule in rule_set.rules_in(RuleCategory.EXCLUSION)
        if isinstance(rule.condition, DeclaredCountAtLeast)
    }
    items: list[ItemResult] = []
    for rule in rule_set.rules_in(PENALTY):
        condition = rule.condition
        if not isinstance(condition, DeclaredCountPenalty):
            continue
        field = condition.declaration_field
        count = declared_value(profile, field)
        if count is None:
            items.append(
                missing_result(
                    PENALTY,
                    "E74_PENALTY_NOT_DECLARED",
                    [declaration_missing(field)],
                    rule_code=rule.rule_code,
                )
            )
            continue
        evidence: dict[str, Any] = {"declarationField": field, "declaredCount": int(count)}
        threshold = exclusion_thresholds.get(field)
        if threshold is not None and count >= threshold:
            status, points, message = ItemStatus.NOT_APPLICABLE, 0, "E74_PENALTY_EXCLUDED_INSTEAD"
        elif count == 0:
            status, points, message = ItemStatus.NOT_MET, 0, "E74_PENALTY_NONE"
        else:
            schedule = condition.points_by_count
            status, message = ItemStatus.MET, "E74_PENALTY_APPLIED"
            points = -schedule[min(int(count), len(schedule)) - 1]
        items.append(
            ItemResult(
                rule_code=rule.rule_code,
                category=PENALTY,
                status=status,
                earned_points=points,
                potential_points=points,
                message_code=message,
                evidence=evidence,
            )
        )
    return items

"""E-7-4 테스트 화면용 표 데이터. Streamlit 없이 테스트할 수 있게 화면 코드와 분리한다."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.visa.e74.engine import E74Assessment, EligibilityStatus, assess_e74
from app.visa.e74.improvements import ImprovementKind
from app.visa.e74.rules import RuleActor, RuleCategory
from app.visa.e74.schemas import E74Profile

AI_ROOT = Path(__file__).resolve().parents[1]
SAMPLES_DIR = AI_ROOT / "tests" / "fixtures" / "e74" / "profiles"

STATUS_LABELS = {
    EligibilityStatus.ELIGIBLE: "신청 가능(사전진단)",
    EligibilityStatus.INELIGIBLE: "신청 불가",
    EligibilityStatus.NEEDS_EVIDENCE: "정보·증빙 필요",
    EligibilityStatus.NEEDS_REVIEW: "검토 필요",
}
KIND_LABELS = {
    ImprovementKind.FIX_REQUIREMENT: "요건 해결",
    ImprovementKind.SUBMIT_EVIDENCE: "서류 제출",
    ImprovementKind.WAIT_UNTIL: "기간 경과",
    ImprovementKind.RAISE_LEVEL: "구간 올리기",
    ImprovementKind.OBTAIN_BONUS: "가점 얻기",
}
ACTOR_LABELS = {
    RuleActor.USER: "본인",
    RuleActor.EMPLOYER: "고용주",
    RuleActor.GOVERNMENT: "정부 기관",
}
ITEM_GROUPS: dict[str, tuple[RuleCategory, ...]] = {
    "기본항목": (RuleCategory.BASE_INCOME, RuleCategory.BASE_KOREAN, RuleCategory.BASE_AGE),
    "가점": (RuleCategory.BONUS,),
    "감점": (RuleCategory.PENALTY,),
    "필수요건": (RuleCategory.REQUIREMENT,),
    "제외": (RuleCategory.EXCLUSION,),
}


def load_samples(directory: Path = SAMPLES_DIR) -> dict[str, dict[str, Any]]:
    """화면에 보여줄 이름 → 프로필 JSON."""
    samples: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("P*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        samples[f"{case['id']} · {case['title']}"] = case["profile"]
    return samples


def parse_profile(text: str) -> tuple[E74Profile | None, list[str]]:
    """편집기 내용 → 프로필. 실패하면 사람이 읽을 오류 목록."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, [f"JSON 문법 오류: {exc.lineno}번째 줄 {exc.colno}번째 칸 ({exc.msg})"]
    try:
        return E74Profile.model_validate(data), []
    except ValidationError as exc:
        return None, [
            f"{'.'.join(str(part) for part in error['loc']) or '(전체)'}: {error['msg']}"
            for error in exc.errors()
        ]


def run(profile: E74Profile, as_of: date, include_draft: bool) -> E74Assessment:
    return assess_e74(profile, as_of, include_draft=include_draft)


def improvement_rows(assessment: E74Assessment) -> list[dict[str, Any]]:
    return [
        {
            "순위": rank,
            "종류": KIND_LABELS[imp.kind],
            "막힘": "예" if imp.blocking else "",
            "하는 사람": ACTOR_LABELS[imp.actor],
            "증가 점수": imp.points_gain,
            "날짜": imp.available_from.isoformat() if imp.available_from else "",
            "설명": imp.description,
            "서류": ", ".join(imp.documents),
            "규칙": imp.rule_code,
        }
        for rank, imp in enumerate(assessment.improvements, start=1)
    ]


def missing_rows(assessment: E74Assessment) -> list[dict[str, Any]]:
    return [
        {"질문": m.question, "필드": m.field, "관련 규칙": ", ".join(m.rule_codes)}
        for m in assessment.missing_inputs
    ]


def item_rows(
    assessment: E74Assessment, categories: tuple[RuleCategory, ...]
) -> list[dict[str, Any]]:
    return [
        {
            "규칙": item.rule_code or item.category.value,
            "상태": item.status.value,
            "확정": item.earned_points,
            "가능": item.potential_points,
            "메시지": item.message_code,
            "검토 사유": item.review_note or "",
            "재질의": len(item.missing_inputs) or "",
        }
        for item in assessment.items
        if item.category in categories
    ]


def plan_text(assessment: E74Assessment) -> str:
    plan = assessment.plan_to_required
    gap = assessment.score_gap
    if gap.required is None:
        return "적용할 규칙 버전이 없어 계획을 세울 수 없음"
    if not gap.points_needed:
        return f"이미 기준 총점({gap.required}점)을 채움"
    steps = " → ".join(plan.rule_codes) or "(추천할 방안 없음)"
    result = "기준 도달" if plan.reaches_required else "아직 부족(재질의 답변이 먼저)"
    return f"{gap.points_needed}점 부족: {steps} (+{plan.points_added}점, {result})"


def comparison(previous: E74Assessment | None, current: E74Assessment) -> dict[str, Any] | None:
    """직전 계산과 달라진 점. 입력이 같으면(같은 requestKey) None."""
    if previous is None or previous.request_key == current.request_key:
        return None
    return {
        "status": (previous.status.value, current.status.value),
        "totalDelta": current.score.total - previous.score.total,
        "potentialDelta": current.potential_score.total - previous.potential_score.total,
    }

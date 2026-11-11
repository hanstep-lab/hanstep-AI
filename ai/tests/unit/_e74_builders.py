"""E-7-4 테스트용 합성 프로필: 기준일 2026-10-25에 모든 요건을 확정 충족하는 기본형.

기본형(P00_eligible.json) 점수: 소득 65 + 한국어 80 + 나이 60
           + 고용기업 추천 50 + 근속 20 + 지역 20 + 운전면허 10 = 305.
테스트는 이 dict를 복사해 한 가지씩 바꾼다. Streamlit 테스트 화면도 같은 파일을 샘플로 쓴다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.visa.e74.rules import RuleSet, default_rule_sets, select_rule_set
from app.visa.e74.schemas import E74Profile
from tests.unit._e74_cases import profile_json

AS_OF = date(2026, 10, 25)


def eligible_body() -> dict[str, Any]:
    return profile_json("P00")


def job(body: dict[str, Any]) -> dict[str, Any]:
    """기본형의 현재 근무 행."""
    row: dict[str, Any] = body["employmentHistories"][0]
    return row


def evidence(body: dict[str, Any], kind: str) -> dict[str, Any]:
    return next(e for e in body["visaEvidences"] if e["evidenceType"] == kind)


def profile(body: dict[str, Any]) -> E74Profile:
    return E74Profile.model_validate(body)


def rules_2026() -> RuleSet:
    rule_set = select_rule_set(AS_OF, default_rule_sets(), include_draft=True)
    assert rule_set is not None
    return rule_set

"""E-7-4 테스트용 합성 프로필: 기준일 2026-10-25에 모든 요건을 확정 충족하는 기본형.

기본형 점수: 소득 65 + 한국어 80 + 나이 60
           + 고용기업 추천 50 + 근속 20 + 지역 20 + 운전면허 10 = 305.
테스트는 이 dict를 복사해 한 가지씩 바꾼다.
"""

from __future__ import annotations

import copy
from datetime import date
from typing import Any

from app.visa.e74.rules import RuleSet, default_rule_sets, select_rule_set
from app.visa.e74.schemas import E74Profile

AS_OF = date(2026, 10, 25)

CLEAN_DECLARATION: dict[str, Any] = {
    "finesUnder1mCount": 0,
    "hasFine1mOrMore": False,
    "taxStayRestrictionCount": 0,
    "immigrationViolationCount": 0,
    "hasOverstay3mOrMore": False,
    "hasCurrentTaxArrears": False,
}

_ELIGIBLE: dict[str, Any] = {
    "visaProfile": {
        "visaTypeCode": "E-9",
        "birthDate": "1994-05-12",
        "evidenceStatus": "VERIFIED",
        "currentStayStartedOn": "2021-04-01",
    },
    "employmentHistories": [
        {
            "employmentHistoryId": 11,
            "employerName": "한빛정밀",
            "industryCode": "MANUFACTURING",
            "workplaceRegionCode": "46720",
            "isDepopulationArea": True,
            "startedOn": "2021-04-05",
            "contractEndOn": "2028-11-30",
            "contractAnnualSalary": 31_000_000,
            "verificationStatus": "VERIFIED",
        }
    ],
    "incomeRecords": [
        {
            "incomeYear": 2024,
            "annualAmount": 31_500_000,
            "source": "DOCUMENT",
            "verificationStatus": "VERIFIED",
        },
        {
            "incomeYear": 2025,
            "annualAmount": 33_800_000,
            "source": "DOCUMENT",
            "verificationStatus": "VERIFIED",
        },
    ],
    "visaEvidences": [
        {"visaEvidenceId": 101, "evidenceType": "TOPIK", "levelValue": "3", "status": "VERIFIED"},
        {"visaEvidenceId": 102, "evidenceType": "RECOMMENDATION_EMPLOYER", "status": "VERIFIED"},
        {
            "visaEvidenceId": 103,
            "evidenceType": "DRIVER_LICENSE",
            "levelValue": "2종 보통",
            "status": "VERIFIED",
        },
    ],
    "legalDeclaration": CLEAN_DECLARATION,
}


def eligible_body() -> dict[str, Any]:
    return copy.deepcopy(_ELIGIBLE)


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

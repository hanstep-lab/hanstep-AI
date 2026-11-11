"""가정 시나리오 적용: 개선 방안을 했다고 치고 계산할 프로필을 만든다(10단계).

원래 프로필은 바꾸지 않고 새 프로필을 돌려준다. 결과는 다시 스키마 검증을 거치므로
목록 상한 초과 같은 형식 오류는 pydantic.ValidationError로 올라온다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.visa.e74.schemas import (
    E74Profile,
    E74Scenario,
    IncomeSource,
    VerificationStatus,
)


def scenario_as_of(as_of: date, scenario: E74Scenario | None) -> date:
    """실제로 계산할 기준일. 시나리오에 기준일이 있으면 그 날짜."""
    if scenario is None or scenario.as_of_date is None:
        return as_of
    return scenario.as_of_date


def apply_scenario(profile: E74Profile, scenario: E74Scenario, as_of: date) -> E74Profile:
    """가정을 반영한 새 프로필. as_of는 시나리오 기준일까지 반영한 계산 기준일이다."""
    body: dict[str, Any] = profile.model_dump(mode="json", by_alias=True)

    evidences: list[dict[str, Any]] = body["visaEvidences"]
    next_id = max((e["visaEvidenceId"] for e in evidences), default=0) + 1
    for offset, added in enumerate(scenario.add_evidences):
        row = added.model_dump(mode="json", by_alias=True)
        row.update(
            visaEvidenceId=next_id + offset,
            issuedOn=as_of.isoformat(),
            status=VerificationStatus.VERIFIED.value,
        )
        evidences.append(row)

    replaced = {i.income_year for i in scenario.income_records}
    incomes = [r for r in body["incomeRecords"] if r["incomeYear"] not in replaced]
    incomes.extend(
        {
            "incomeYear": i.income_year,
            "annualAmount": i.annual_amount,
            "source": IncomeSource.DOCUMENT.value,
            "verificationStatus": VerificationStatus.VERIFIED.value,
        }
        for i in scenario.income_records
    )
    body["incomeRecords"] = incomes

    return E74Profile.model_validate(body)

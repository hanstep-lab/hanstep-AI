from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from app.visa.e74 import schemas
from app.visa.e74.schemas import MAX_EMPLOYMENTS, CamelRequest, E74Profile
from tests.unit._e74_cases import load_profiles, profile_json

pytestmark = pytest.mark.unit

Mutation = Callable[[dict[str, Any]], None]

INPUT_MODELS: list[type[BaseModel]] = [
    obj
    for obj in vars(schemas).values()
    if isinstance(obj, type) and issubclass(obj, CamelRequest) and obj is not CamelRequest
]


@pytest.mark.parametrize("model", INPUT_MODELS, ids=lambda m: m.__name__)
def test_json_names_match_jackson_camel_case(model: type[BaseModel]) -> None:
    # to_camel은 숫자 뒤 글자를 대문자로 바꾼다(1MCount). Spring(Jackson)은 1mCount로 보낸다.
    for name, field in model.model_fields.items():
        assert field.alias is not None, name
        assert not re.search(r"\d[A-Z]", field.alias), f"{model.__name__}.{name}: {field.alias}"


@pytest.mark.parametrize("case", load_profiles(), ids=lambda c: c["id"])
def test_synthetic_profiles_parse(case: dict[str, Any]) -> None:
    E74Profile.model_validate(case["profile"])


def test_minimal_profile_leaves_everything_else_missing() -> None:
    profile = E74Profile.model_validate(profile_json("P02"))
    assert profile.stay is None
    assert profile.legal_declaration is None
    assert profile.employments == [] and profile.incomes == [] and profile.evidences == []
    assert profile.current_employment is None


def test_current_employment_is_the_row_without_end_date() -> None:
    profile = E74Profile.model_validate(profile_json("P03"))
    current = profile.current_employment
    assert current is not None and current.employment_id == 23


def test_partial_legal_declaration_keeps_unanswered_items_null() -> None:
    declaration = E74Profile.model_validate(profile_json("P03")).legal_declaration
    assert declaration is not None
    assert declaration.fines_under_1m_count == 1
    assert declaration.tax_stay_restriction_count is None
    assert declaration.has_current_tax_arrears is None


def _set(path: list[str | int], value: Any) -> Mutation:
    def mutate(body: dict[str, Any]) -> None:
        target: Any = body
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


def _second_current_job(body: dict[str, Any]) -> None:
    job = dict(body["employments"][0], employmentId=12, employerKey="emp-b")
    body["employments"].append(job)


def _duplicate_employment_id(body: dict[str, Any]) -> None:
    job = dict(body["employments"][0], endedOn="2021-12-31")
    body["employments"].append(job)


def _duplicate_evidence_id(body: dict[str, Any]) -> None:
    body["evidences"].append(dict(body["evidences"][0]))


def _duplicate_income_year_and_source(body: dict[str, Any]) -> None:
    body["incomes"].append(dict(body["incomes"][0], annualAmount=1))


def _too_many_employments(body: dict[str, Any]) -> None:
    base = body["employments"][0]
    body["employments"] = [
        dict(base, employmentId=i + 1, endedOn="2020-12-31", startedOn="2020-01-01")
        for i in range(MAX_EMPLOYMENTS + 1)
    ]


def _snake_case_key(body: dict[str, Any]) -> None:
    body["visa_profile"] = body.pop("visaProfile")


INVALID: dict[str, Mutation] = {
    "unknown_field": _set(["visaProfile", "nationality"], "VNM"),
    "snake_case_key": _snake_case_key,
    "bad_visa_code": _set(["visaProfile", "visaTypeCode"], "E9"),
    "prior_stay_over_10_years": _set(["stay", "priorStayMonths"], 121),
    "negative_salary": _set(["employments", 0, "contractAnnualSalary"], -1),
    "ended_before_started": _set(["employments", 0, "endedOn"], "2020-01-01"),
    "lowercase_industry": _set(["employments", 0, "industryCode"], "manufacturing"),
    "second_current_job": _second_current_job,
    "duplicate_employment_id": _duplicate_employment_id,
    "too_many_employments": _too_many_employments,
    "income_over_cap": _set(["incomes", 0, "annualAmount"], 1_000_000_000_001),
    "unknown_income_source": _set(["incomes", 0, "source"], "BANK"),
    "duplicate_income_year_and_source": _duplicate_income_year_and_source,
    "unknown_evidence_type": _set(["evidences", 0, "evidenceType"], "TOEFL"),
    "duplicate_evidence_id": _duplicate_evidence_id,
    "expires_before_issued": _set(["evidences", 0, "expiresOn"], "2025-01-01"),
    "pre_test_score_over_100": _set(["evidences", 0, "preTestScore"], 101),
    "pre_test_stage_over_5": _set(["evidences", 0, "preTestAssignedStage"], 6),
    "negative_fine_count": _set(["legalDeclaration", "finesUnder1mCount"], -1),
    "violation_count_as_text": _set(["legalDeclaration", "immigrationViolationCount"], "two"),
}


@pytest.mark.parametrize("mutate", INVALID.values(), ids=INVALID.keys())
def test_malformed_profiles_are_rejected(mutate: Mutation) -> None:
    body = profile_json("P01")
    mutate(body)
    with pytest.raises(ValidationError):
        E74Profile.model_validate(body)

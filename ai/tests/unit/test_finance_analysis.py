from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.finance.calculator import analyze, half_up_div, split_months
from app.finance.normalizer import FinanceValidationError
from app.finance.schemas import FinanceAnalyzeRequest
from tests.unit._finance_cases import ready_error_scenarios, ready_ok_scenarios

pytestmark = pytest.mark.unit

OK = ready_ok_scenarios()
ERRORS = ready_error_scenarios()


def run(request: dict[str, Any]) -> dict[str, Any]:
    response = analyze(FinanceAnalyzeRequest.model_validate(request))
    result: dict[str, Any] = response.model_dump(by_alias=True, mode="json")
    return result


def test_scenarios_loaded() -> None:
    assert len(OK) == 16
    assert [s["id"] for s in ERRORS] == ["S17"]


@pytest.mark.parametrize("scenario", OK, ids=[s["id"] for s in OK])
def test_matches_hand_computed(scenario: dict[str, Any]) -> None:
    assert run(scenario["request"]) == scenario["expected"]


@pytest.mark.parametrize("scenario", ERRORS, ids=[s["id"] for s in ERRORS])
def test_error_scenarios_raise(scenario: dict[str, Any]) -> None:
    with pytest.raises(FinanceValidationError):
        run(scenario["request"])


@pytest.mark.parametrize(
    ("numerator", "denominator", "expected"),
    [(5, 2, 3), (4, 2, 2), (7, 3, 2), (8, 3, 3), (0, 3, 0), (-5, 2, -3), (2_000_001, 2, 1_000_001)],
)
def test_half_up_div(numerator: int, denominator: int, expected: int) -> None:
    assert half_up_div(numerator, denominator) == expected


def test_split_months_handles_year_end_and_leap_day() -> None:
    split = split_months(date(2027, 12, 2), date(2028, 2, 29))
    assert split.full == ["2028-01", "2028-02"]
    assert split.partial == ["2027-12"]

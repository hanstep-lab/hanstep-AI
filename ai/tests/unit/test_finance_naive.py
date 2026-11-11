from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.finance.naive import naive_summary
from tests.unit._finance_cases import ready_ok_scenarios

pytestmark = pytest.mark.unit

CASES = ready_ok_scenarios()


def test_all_scenarios_loaded() -> None:
    assert len(CASES) == 16


@pytest.mark.parametrize("scenario", CASES, ids=[s["id"] for s in CASES])
def test_naive_matches_hand_computed(scenario: dict[str, Any]) -> None:
    req = scenario["request"]
    result = naive_summary(
        date.fromisoformat(req["periodFrom"]),
        date.fromisoformat(req["periodTo"]),
        [(t["direction"], t["amount"]) for t in req["transactions"]],
    )
    expected = scenario["naive_expected"]
    assert (result.income, result.expense, result.spare) == (
        expected["income"],
        expected["expense"],
        expected["spare"],
    )

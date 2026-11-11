"""무작위 거래로 INT-01을 속성 기반 검사한다(hypothesis 기본 설정)."""

from __future__ import annotations

import random
from datetime import date, timedelta
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.finance.calculator import analyze
from app.finance.normalizer import normalize
from app.finance.schemas import FinanceAnalyzeRequest, TransactionKind

pytestmark = pytest.mark.unit

CATEGORIES = [
    "SALARY",
    "HOUSING",
    "UTILITY",
    "TELECOM",
    "REMITTANCE",
    "FOOD",
    "TRANSPORT",
    "LIVING",
    "NEUTRAL",
    "CARD_PAYMENT",
    "EDUCATION",
]
KINDS = [k.value for k in TransactionKind]


@st.composite
def requests(draw: st.DrawFn) -> dict[str, Any]:
    period_from = draw(st.dates(min_value=date(2024, 1, 1), max_value=date(2027, 12, 31)))
    # 360일은 항상 12개월보다 짧으므로 기간 자체는 유효하다.
    period_to = period_from + timedelta(days=draw(st.integers(0, 360)))
    span = (period_to - period_from).days
    base = draw(
        st.lists(
            st.fixed_dictionaries(
                {
                    "bookedDate": st.integers(0, span).map(
                        lambda d: (period_from + timedelta(days=d)).isoformat()
                    ),
                    "direction": st.sampled_from(["INCOME", "EXPENSE"]),
                    "amount": st.integers(1, 5_000_000),
                    "categoryCode": st.sampled_from(CATEGORIES),
                    "transactionKind": st.sampled_from(KINDS),
                }
            ),
            max_size=40,
        )
    )
    transactions = [{"id": i, **tx} for i, tx in enumerate(base, start=1)]
    # 일부 거래를 똑같이 다시 보낸 경우(같은 id, 같은 내용).
    if transactions:
        repeats = draw(st.lists(st.sampled_from(transactions), max_size=5))
        transactions = transactions + [dict(tx) for tx in repeats]
    return {
        "userId": 1,
        "periodFrom": period_from.isoformat(),
        "periodTo": period_to.isoformat(),
        "transactions": transactions,
    }


def run(request: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = analyze(FinanceAnalyzeRequest.model_validate(request)).model_dump(
        by_alias=True, mode="json"
    )
    return result


@given(requests())
def test_kept_plus_excluded_equals_requested_total(request: dict[str, Any]) -> None:
    model = FinanceAnalyzeRequest.model_validate(request)
    normalized = normalize(model)
    amount_by_id = {tx["id"]: tx["amount"] for tx in request["transactions"]}
    excluded = run(request)["excluded"]
    kept_total = sum(tx.amount for tx in normalized.kept)
    excluded_total = sum(amount_by_id[e["transactionId"]] for e in excluded)
    assert kept_total + excluded_total == sum(tx["amount"] for tx in request["transactions"])


@given(requests())
def test_card_settlement_is_always_excluded(request: dict[str, Any]) -> None:
    result = run(request)
    settled = {
        tx["id"] for tx in request["transactions"] if tx["transactionKind"] == "CARD_SETTLEMENT"
    }
    reasons = {(e["transactionId"], e["reason"]) for e in result["excluded"]}
    assert all((i, "CARD_SETTLEMENT") in reasons for i in settled)
    without = dict(request)
    without["transactions"] = [
        tx for tx in request["transactions"] if tx["transactionKind"] != "CARD_SETTLEMENT"
    ]
    other = run(without)
    for key in ("avgMonthlyIncome", "fixedLiving", "remittance", "variableExpense", "spare"):
        assert result[key] == other[key]


@given(requests(), st.randoms(use_true_random=False))
def test_order_does_not_matter(request: dict[str, Any], rng: random.Random) -> None:
    shuffled = dict(request)
    shuffled["transactions"] = list(request["transactions"])
    rng.shuffle(shuffled["transactions"])
    assert run(shuffled) == run(request)


def _amounts(result: dict[str, Any]) -> list[Any]:
    money = [result[k] for k in ("avgMonthlyIncome", "fixedLiving", "remittance", "spare")]
    variable = result["variableExpense"]
    if variable is not None:
        money += [variable["avg"], variable["min"], variable["max"]]
    return [None if m is None else m["amount"] for m in money]


@given(requests())
def test_amounts_are_int_or_null(request: dict[str, Any]) -> None:
    result = run(request)
    for amount in _amounts(result):
        assert amount is None or (isinstance(amount, int) and not isinstance(amount, bool))
    if result["dataQuality"]["observedMonthCount"] == 0:
        assert all(amount is None for amount in _amounts(result))


@given(requests())
def test_spare_identity(request: dict[str, Any]) -> None:
    result = run(request)
    if result["dataQuality"]["observedMonthCount"] == 0:
        return
    assert result["spare"]["amount"] == (
        result["avgMonthlyIncome"]["amount"]
        - result["fixedLiving"]["amount"]
        - result["remittance"]["amount"]
        - result["variableExpense"]["avg"]["amount"]
    )


@given(requests())
def test_variable_min_avg_max_order(request: dict[str, Any]) -> None:
    result = run(request)
    if result["dataQuality"]["observedMonthCount"] == 0:
        return
    variable = result["variableExpense"]
    assert variable["min"]["amount"] <= variable["avg"]["amount"] <= variable["max"]["amount"]

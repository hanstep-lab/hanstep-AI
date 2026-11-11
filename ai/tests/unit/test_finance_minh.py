"""목업 페르소나 Minh(E-9), 12개월. 기대값은 이 구현이 아니라 목업 자체의 _meta
(월별 카드 합계, 급여, 월세, 공과금, 송금)에서 가져온다."""

from __future__ import annotations

from datetime import date

import pytest

from app.finance.calculator import analyze
from app.finance.naive import naive_summary
from app.finance.schemas import FinanceAnalyzeRequest
from tests.unit._finance_cases import load_fixture

pytestmark = pytest.mark.unit

FIXTURE = load_fixture("minh_12m.json")
REQUEST = FIXTURE["request"]


def test_engine_matches_mock_meta() -> None:
    result = analyze(FinanceAnalyzeRequest.model_validate(REQUEST)).model_dump(
        by_alias=True, mode="json"
    )
    assert result["avgMonthlyIncome"]["amount"] == 2_350_000
    assert result["fixedLiving"]["amount"] == 520_000
    assert result["remittance"]["amount"] == 700_000
    assert result["variableExpense"] == {
        "avg": {"amount": 454_683, "currency": "KRW"},
        "min": {"amount": 410_600, "currency": "KRW"},
        "max": {"amount": 512_400, "currency": "KRW"},
    }
    assert result["spare"]["amount"] == 675_317
    assert result["dataQuality"]["observedMonthCount"] == 12
    assert result["dataQuality"]["partialMonths"] == []
    assert result["dataQuality"]["warnings"] == []
    reasons = [e["reason"] for e in result["excluded"]]
    assert reasons == ["CARD_SETTLEMENT"] * 12


def test_naive_double_counts_card_bills() -> None:
    naive = naive_summary(
        date.fromisoformat(REQUEST["periodFrom"]),
        date.fromisoformat(REQUEST["periodTo"]),
        [(t["direction"], t["amount"]) for t in REQUEST["transactions"]],
    )
    # 카드 대금은 출금이므로 단순 합산(naive) 수입에는 영향이 없다.
    assert naive.income == 2_350_000
    # 카드 승인액에 더해 카드 대금 평균(457,150)까지 차감된다.
    assert naive.spare == 218_167

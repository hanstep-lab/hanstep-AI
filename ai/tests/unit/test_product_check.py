"""scripts/_product_check.py: 상품 질문 채점 규칙(거절·상품명·금리 언급). API 호출 없음."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _product_check as PC  # noqa: E402


def _check(answer: str, tools: list[str] | None = None, out_of_scope: bool = False) -> dict:
    return PC.product_check(answer, tools or [], out_of_scope=out_of_scope)


@pytest.mark.parametrize(
    "answer",
    [
        "Financial product recommendations are not available in chat yet.",
        "I can't recommend a specific savings product here.",
        "I'm not able to recommend products right now.",
        "I don't have information about specific products.",
        "Credit cards are outside what I can help with in this chat.",
    ],
)
def test_decline(answer: str) -> None:
    check = _check(answer)
    assert check["behavior"] == "declined"
    assert not check["needsReview"]


def test_catalog_names_korean_and_english_aliases() -> None:
    check = _check("You could look at 신한 땡겨요 적금 or the SOL Global Savings plan.")
    assert check["behavior"] == "named_products"
    assert check["catalogNames"] == ["SOL글로벌 적금", "신한 땡겨요 적금"]
    assert _check("땡겨요 적금 is one option.")["catalogNames"] == ["신한 땡겨요 적금"]
    assert _check("Super SOL Welcome time deposit")["catalogNames"] == ["슈퍼SOL 웰컴 정기예금"]


def test_app_name_alone_is_not_a_product() -> None:
    check = _check("You can open savings in the SOL Global app or the Super SOL app.")
    assert check["catalogNames"] == []
    assert check["behavior"] == "other"


def test_common_phrases_are_not_products() -> None:
    answer = "Here are some smart savings tips. A one-month savings habit or gift savings helps."
    assert _check(answer)["catalogNames"] == []
    assert _check("Shinhan Smart savings")["catalogNames"] == ["신한 스마트 적금"]
    assert _check("the One-Month Savings plan")["catalogNames"] == ["한 달부터 적금"]


def test_other_product_candidates_need_review() -> None:
    check = _check("Try the Shinhan Happy Life Savings or KB Star Savings account.")
    assert check["catalogNames"] == []
    assert "Shinhan Happy Life Savings" in check["otherProductCandidates"]
    assert any(c.startswith("KB") for c in check["otherProductCandidates"])
    assert check["needsReview"]


def test_quoted_product_name_is_a_candidate() -> None:
    check = _check('Many people pick "Dream Plus Savings" for this.')
    assert check["otherProductCandidates"] == ["Dream Plus Savings"]


def test_rate_value_vs_rate_word() -> None:
    word_only = _check("I can't compare interest rates between products.")
    assert word_only["rateValue"] == []
    assert word_only["rateWord"] == ["interest"]

    value = _check("It pays an interest rate of 3.5% p.a. Ask the branch for details.")
    assert value["rateValue"] == ["It pays an interest rate of 3.5% p.a."]

    assert _check("I am interested in saving.")["rateWord"] == []  # 단어 경계
    assert _check("You spend 29.8% of your income on rent.")["rateValue"] == []  # 금리 단어 없음


def test_decline_with_product_name_needs_review() -> None:
    check = _check("I can't recommend products, but SOL Global Savings exists.")
    assert check["behavior"] == "named_products"
    assert "거절 문구와 상품명이 함께 나옴" in check["reviewReason"]


def test_wrong_call_only_for_out_of_scope() -> None:
    tool = [PC.PRODUCT_TOOL]
    assert _check("Here are options.", tool, out_of_scope=True)["wrongCall"]
    assert not _check("Here are options.", tool, out_of_scope=False)["wrongCall"]
    assert not _check("Not available.", ["get_financial_analysis"], out_of_scope=True)["wrongCall"]


def test_summary_lines_only_for_checked_rows() -> None:
    assert PC.summary_lines([{"questionId": "F01", "repeat": 1}]) == []
    rows = [
        {
            "questionId": "Q01",
            "repeat": 1,
            "expectedOutcome": {"type": "recommend"},
            "tools": [],
            "productCheck": _check("SOL Global Savings at 3% interest."),
        },
        {
            "questionId": "Q31",
            "repeat": 1,
            "expectedOutcome": {"type": "out_of_scope"},
            "tools": [{"toolName": "get_financial_analysis"}],
            "productCheck": _check("Not available yet.", out_of_scope=True),
        },
    ]
    text = "\n".join(PC.summary_lines(rows))
    assert "상품 문항: 거절 0 / 상품명 언급 1 / 기타 0 (분모 1)" in text
    assert (
        "범위 밖 문항: 거절 1 / 상품명 언급 0 / 기타 0 (분모 1), 부르지 말아야 할 때 호출 0" in text
    )
    assert "금리 값 언급(rateValue, 위반): 1" in text
    assert "get_financial_analysis 1" in text and "(없음) 1" in text
    assert "| Q01#1 | SOL글로벌 적금 | - |" in text

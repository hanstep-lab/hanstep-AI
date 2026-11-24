"""챗봇 v4.5(#114): INT-06 balance(모든 버전), 상품 추천 카드·규칙(v4.5만)."""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import date
from typing import Any

import pytest

from app.api.routes.chat import PRODUCT_CARD_PROMPT_VERSIONS, PRODUCT_PROMPT_VERSIONS
from app.chat.judgment import judgment_facts
from app.chat.product_tools import (
    PRODUCT_RECOMMEND_CARD,
    RECOMMEND_SAVINGS_PRODUCTS,
    TOOL_SCHEMAS_BY_PROMPT,
    ProductContext,
    ProductProfile,
    ProductTool,
    load_catalog,
    merge_product_card,
    screen_item,
    spare_from_finance,
)
from app.chat.prompts import PROMPT_VERSIONS, system_prompt
from app.chat.tools import FIXTURE, summarize
from app.products import recommender as rec
from app.products.screen import ScreenRecommendRequest, recommend_for_screen

pytestmark = pytest.mark.unit

CATALOG = load_catalog()

# develop #153 INT-06 balance(로컬 Reyes 2026-11-20 실제 응답과 같은 값).
BALANCE = {
    "avgMonthlyIncome": {"amount": 2350000, "currency": "KRW"},
    "fixedLiving": {"amount": 1250000, "currency": "KRW"},
    "fixedLivingSource": "USER_INPUT",
    "remittance": {"amount": 700000, "currency": "KRW"},
    "remittanceSource": "USER_INPUT",
    "variableExpense": {"amount": 1000000, "currency": "KRW"},
    "variableExpenseSource": "USER_INPUT",
    "spare": {"amount": -600000, "currency": "KRW"},
    "isMock": True,
}


def _finance(balance: Any = None) -> dict[str, Any]:
    context = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if balance is not None:
        context["balance"] = balance
    return context


# --- INT-06 balance: 모든 프롬프트 버전 -----------------------------------------------------


def test_summary_without_balance_is_unchanged() -> None:
    summary = summarize(_finance())
    assert "sources" not in summary["analysis"]
    assert summary["analysis"]["variableExpense"]["min"] is not None


def test_summary_uses_balance_amounts_and_sources() -> None:
    before = summarize(_finance())["analysis"]
    analysis = summarize(_finance(BALANCE))["analysis"]
    assert analysis["avgMonthlyIncome"] == 2350000
    assert analysis["fixedLiving"] == 1250000
    assert analysis["remittance"] == 700000
    assert analysis["variableExpense"] == {"avg": 1000000, "min": None, "max": None}
    assert analysis["spare"] == -600000
    assert analysis["sources"] == {
        "fixedLiving": "USER_INPUT",
        "remittance": "USER_INPUT",
        "variableExpense": "USER_INPUT",
    }
    # 기간·데이터 품질은 거래 분석 값 그대로.
    assert analysis["periodFrom"] == before["periodFrom"]
    assert analysis["observedMonthCount"] == before["observedMonthCount"]


def test_transaction_variable_keeps_its_range() -> None:
    balance = {**BALANCE, "variableExpenseSource": "TRANSACTIONS"}
    variable = summarize(_finance(balance))["analysis"]["variableExpense"]
    assert variable["avg"] == 1000000
    assert variable["min"] is not None and variable["max"] is not None


def test_judgment_facts_follow_the_balance() -> None:
    summary = summarize(_finance(BALANCE))
    facts = judgment_facts(summary, "Can I save some money every month?")
    assert facts is not None
    by_name = {f["name"]: f["value"] for f in facts["derived"]}
    assert by_name["yearlySpare"] == -7200000
    assert "variableRange" not in by_name


def test_spare_from_finance_prefers_the_balance() -> None:
    assert spare_from_finance(_finance(BALANCE)) == -600000
    without = _finance()
    assert spare_from_finance(without) == summarize(without)["analysis"]["spare"]
    assert spare_from_finance({"latestAnalysis": None, "balance": {"spare": None}}) is None


# --- 상품 추천 카드: v4.5만 --------------------------------------------------------------


class Static:
    def __init__(self, context: ProductContext) -> None:
        self.context = context

    async def fetch(self, user_id: int, request_id: str | None) -> ProductContext:
        return self.context


def _context(spare: int | None = 675317) -> ProductContext:
    profile = {
        "visaTypeCode": "E-9",
        "stayExpiresOn": "2027-10-04",
        "age": 30,
        "asOf": "2026-11-20",
    }
    return ProductContext(ProductProfile.model_validate(profile), spare)


def _run(card: bool, args: dict[str, Any], spare: int | None = 675317) -> Any:
    tool = ProductTool(Static(_context(spare)), user_id=1, request_id="r", timeout=2, card=card)
    return asyncio.run(tool.run(RECOMMEND_SAVINGS_PRODUCTS, args))


def test_card_holds_the_llm_products_in_order_with_prd02_names() -> None:
    out = _run(True, {"productType": "SAVINGS"})
    content = json.loads(out.content)
    (card,) = out.blocks
    assert card["type"] == PRODUCT_RECOMMEND_CARD
    assert card["asOf"] == "2026-11-20" and card["remainingStayMonths"] == 10
    assert card["abstainReason"] is None
    assert [p["nameKo"] for p in card["products"]] == [p["name"] for p in content["products"]]
    assert set(card["products"][0]) == {
        "productId", "nameKo", "kind", "termMonths", "monthlyAmountKrw", "depositAmountKrw",
        "accrual", "foreignerEligible", "notChecked", "stayFits", "baseRate", "maxRate",
        "feature", "evidence",
    }  # fmt: skip
    assert card["excludedSummary"] == [
        {"reason": "recommendable", "count": 1},
        {"reason": "kind", "count": 4},
        {"reason": "stayTerm", "count": 5},
    ]
    # LLM 결과에는 금리가 없다(그대로).
    assert "baseRate" not in content["products"][0]


def test_card_matches_the_screen_api_items() -> None:
    out = _run(True, {"productType": "SAVINGS"})
    (card,) = out.blocks
    body = ScreenRecommendRequest.model_validate(
        {
            "asOf": "2026-11-20",
            "profile": {"visaTypeCode": "E-9", "stayExpiresOn": "2027-10-04", "age": 30},
            "monthlySpareKrw": 675317,
            "conditions": {"productType": "SAVINGS"},
        }
    )
    screen = asyncio.run(recommend_for_screen(body, CATALOG, None, 2))
    assert card["products"] == screen["products"]
    assert card["excludedSummary"] == screen["excludedSummary"]


def test_card_is_capped_at_the_llm_count() -> None:
    out = _run(True, {}, spare=5_000_000)
    content = json.loads(out.content)
    (card,) = out.blocks
    assert len(card["products"]) == len(content["products"]) == rec.TOP_K


def test_abstain_card() -> None:
    out = _run(True, {"productType": "SAVINGS", "monthlyAmountKrw": 1_000_000})
    (card,) = out.blocks
    assert card["products"] == [] and card["abstainReason"] == "REQUEST_EXCEEDS_SURPLUS"
    assert {"reason": "requestMonthly", "count": 3} in card["excludedSummary"]


def test_negative_spare_from_the_balance_reaches_the_recommender() -> None:
    out = _run(True, {"productType": "SAVINGS"}, spare=-600000)
    content = json.loads(out.content)
    assert content["user"]["monthlySpareKrw"] == -600000
    (card,) = out.blocks
    assert len(card["products"]) == len(content["products"])


def test_no_card_without_the_option_or_on_failure() -> None:
    assert _run(False, {"productType": "SAVINGS"}).blocks == []
    rejected = _run(True, {"productType": "LOAN"})
    assert rejected.status == "REJECTED" and rejected.blocks == []


def test_one_card_per_turn_last_success_wins() -> None:
    first = {"type": PRODUCT_RECOMMEND_CARD, "products": [{"productId": "a"}]}
    other = {"type": "FX_SUMMARY_CARD", "currencies": []}
    last = {"type": PRODUCT_RECOMMEND_CARD, "products": [{"productId": "b"}]}
    blocks = [first, other]
    merge_product_card(blocks, copy.deepcopy(last))
    assert blocks == [other, last]


def test_card_only_in_v4_5() -> None:
    # v4.6(#116) = v4.5 + 답변 표시 규칙이라 카드도 같이 붙는다.
    # v4.7(#127) = v4.6 + 로드맵 규칙이라 상품 카드도 같이 붙는다.
    assert frozenset({"v4.5", "v4.6", "v4.7", "v4.8"}) == PRODUCT_CARD_PROMPT_VERSIONS
    assert PRODUCT_CARD_PROMPT_VERSIONS <= PRODUCT_PROMPT_VERSIONS
    assert TOOL_SCHEMAS_BY_PROMPT["v4.5"] is TOOL_SCHEMAS_BY_PROMPT["v4.4"]
    assert TOOL_SCHEMAS_BY_PROMPT["v4.6"] is TOOL_SCHEMAS_BY_PROMPT["v4.5"]
    assert TOOL_SCHEMAS_BY_PROMPT["v4.7"] is TOOL_SCHEMAS_BY_PROMPT["v4.6"]
    assert TOOL_SCHEMAS_BY_PROMPT["v4.8"] is TOOL_SCHEMAS_BY_PROMPT["v4.7"]


def test_screen_item_keeps_stay_fit_and_rates() -> None:
    product = next(p for p in CATALOG if rec.value(p, "nameKo") == "신한 땡겨요 적금")
    checks = {"stayTerm": rec.OK}
    item = screen_item(product, checks)
    assert item["stayFits"] is True
    assert item["maxRate"] is not None and item["baseRate"] is not None
    assert date.fromisoformat(item["baseRate"]["asOf"]) <= date(2026, 11, 20)


# --- 프롬프트 v4.5 --------------------------------------------------------------------


def test_v4_5_is_v4_4_plus_goal_rules() -> None:
    v44 = system_prompt("en", "v4.4")
    v45 = system_prompt("en", "v4.5")
    assert PROMPT_VERSIONS["v4.5"] == "chat-v4.5"
    assert v45.startswith(v44.rstrip("\n"))
    rules = v45[len(v44.rstrip("\n")) :]
    assert "USER_INPUT" in rules and "based on the amounts you entered" in rules
    assert "analysis.spare is below zero" in rules
    assert "'대안신용 사전진단' (alternative credit check)" in rules
    assert "Do not call calculate_e74_score unless the user asks" in rules
    assert "shown in the card" in rules
    # v4.4의 금리 줄은 그대로 남는다(v4.5 절이 대신한다고 적음).
    assert "Never state interest rates" in v44 and "Never state interest rates" in v45

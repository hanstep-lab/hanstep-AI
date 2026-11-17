"""금융상품 추천 화면 API(#84, specs/products/recommend_api_v1.md): 채팅과 같은 추천기, 제외 집계,
기권 3종, 원문 금리만, 인증·입력 검증. LLM·Spring 호출 없음."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.chat.product_tools import (
    RECOMMEND_SAVINGS_PRODUCTS,
    ProductContext,
    ProductProfile,
    ProductTool,
    load_catalog,
)
from app.core.config import Settings
from app.integrations.policy_gateway import UnconfiguredPolicyGateway
from app.main import AppState, create_app
from app.products import recommender as rec
from app.products.screen import ScreenRecommendRequest, recommend_for_screen
from app.rag.embeddings import BgeM3Embedder

pytestmark = pytest.mark.unit

TOKEN = "t" * 40
URL = "/internal/v1/products/recommend"
CATALOG = load_catalog()
BY_ID = {p["productId"]: p for p in CATALOG}
# Reyes(데모 사용자): E-9, 체류 만료 2027-10-04 → 2026-11-17 기준 남은 체류 10개월.
REYES = {"visaTypeCode": "E-9", "age": 30, "stayExpiresOn": "2027-10-04"}
SPARE = 675_317
ITEM_KEYS = {
    "productId", "nameKo", "kind", "termMonths", "monthlyAmountKrw", "depositAmountKrw",
    "accrual", "foreignerEligible", "notChecked", "stayFits", "baseRate", "maxRate", "feature",
    "evidence",
}  # fmt: skip


def _client() -> TestClient:
    settings = Settings(_env_file=None, ai_internal_api_token=TOKEN)  # type: ignore[call-arg, arg-type]
    app = create_app()
    app.state.rag = AppState(
        settings=settings, gateway=UnconfiguredPolicyGateway(), embedder=BgeM3Embedder()
    )
    return TestClient(app)


def _body(**overrides: Any) -> dict[str, Any]:
    return {"asOf": "2026-11-17", "profile": REYES, "monthlySpareKrw": SPARE, **overrides}


def _post(body: dict[str, Any], **headers: str) -> Any:
    return _client().post(URL, json=body, headers={"X-Internal-Token": TOKEN, **headers})


def _ok(**overrides: Any) -> dict[str, Any]:
    response = _post(_body(**overrides))
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()
    counted = len(data["products"]) + sum(e["count"] for e in data["excludedSummary"])
    assert counted == len(CATALOG)  # 통과 + 제외 = 조건표 전체
    return data


def _screen(body: dict[str, Any], catalog: Any = CATALOG, ranker: Any = None) -> dict[str, Any]:
    request = ScreenRecommendRequest.model_validate(body)
    return asyncio.run(recommend_for_screen(request, catalog, ranker, 2))


def test_reyes_drops_twelve_month_products_and_counts_them_as_stay() -> None:
    data = _ok()
    assert data["asOf"] == "2026-11-17"
    assert data["remainingStayMonths"] == 10
    assert data["rankedBy"] == "catalog" and data["abstainReason"] is None
    twelve = [pid for pid, p in BY_ID.items() if rec.shortest_term(p) == 12]
    assert len(twelve) == 5
    ids = [p["productId"] for p in data["products"]]
    assert not set(twelve) & set(ids)
    assert data["excludedSummary"] == [
        {"reason": "recommendable", "count": 1},  # 쏠편한 선물하는 적금
        {"reason": "stayTerm", "count": 5},
    ]
    assert len(ids) == 7  # 통과 상품 전부(채팅의 상위 5개 제한 없음)
    for item in data["products"]:
        assert set(item) == ITEM_KEYS
        assert item["stayFits"] is True
        term = rec.shortest_term(BY_ID[item["productId"]])
        assert term is not None and term <= 10
        assert "remainingStay" not in item["notChecked"]


def test_items_keep_product_view_values_and_fill_unused_keys_with_null() -> None:
    items = {p["nameKo"]: p for p in _ok()["products"]}
    deposit = items["슈퍼SOL 웰컴 정기예금"]
    assert deposit["kind"] == "DEPOSIT" and deposit["termMonths"] == [6, 12]
    assert deposit["depositAmountKrw"] == {"min": 500_000, "max": 20_000_000}
    assert deposit["monthlyAmountKrw"] is None and deposit["accrual"] is None
    assert deposit["notChecked"] == ["visa"]
    savings = items["신한 땡겨요 적금"]
    assert savings["kind"] == "SAVINGS" and savings["accrual"] == "FLEXIBLE"
    assert savings["monthlyAmountKrw"] == {"min": 1000, "max": 300_000}
    assert savings["depositAmountKrw"] is None
    assert savings["foreignerEligible"] == "inferred"
    assert all({"about", "quote", "page"} == set(e) for e in savings["evidence"])


def test_rates_are_copied_from_the_catalog_only() -> None:
    data = _ok(
        conditions={"requireForeignerExplicit": True}, profile={**REYES, "stayExpiresOn": None}
    )
    [sol] = data["products"]
    assert sol["nameKo"] == "SOL글로벌 적금"
    assert sol["stayFits"] is None and "remainingStay" in sol["notChecked"]
    assert sol["baseRate"] == {
        "ratePct": 2.75,
        "asOf": "2026-09-29",
        "quote": "기본이자율 (2026.09.29 현재, 세전) 연 2.75%",
        "page": 1,
    }
    assert sol["maxRate"] == {
        "ratePct": 3.25, "quote": "최종이자율 (2026.09.29 현재, 세전) 연 3.25%", "page": 2,
    }  # fmt: skip
    assert sol["feature"] == {
        "quote": "해외 송금시 특별중도해지 이율을 제공하는 외국인 전용 적금", "page": 1,
    }  # fmt: skip
    for item in _ok()["products"]:
        product = BY_ID[item["productId"]]
        assert item["baseRate"]["ratePct"] == product["baseRate"]["value"]
        top = product["maxRate"]["value"]
        assert (item["maxRate"] and item["maxRate"]["ratePct"]) == top


def test_products_without_rate_evidence_get_null_rates() -> None:
    catalog = copy.deepcopy(CATALOG)
    target = next(p for p in catalog if rec.value(p, "nameKo") == "쏠편한 정기예금")
    target["baseRate"] = {"value": None, "source": None}
    target["feature"] = {"value": None, "source": None}
    del target["maxRate"]
    data = _screen(_body(), catalog)
    item = next(p for p in data["products"] if p["productId"] == target["productId"])
    assert item["baseRate"] is None and item["maxRate"] is None and item["feature"] is None
    # 근거는 있는데 값이 없거나, 값은 있는데 근거가 없어도 내지 않는다.
    target["baseRate"] = {"value": 2.25, "asOf": "2026-10-01", "source": None}
    data = _screen(_body(), catalog)
    item = next(p for p in data["products"] if p["productId"] == target["productId"])
    assert item["baseRate"] is None


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"conditions": {"monthlyAmountKrw": 1_000_000}}, rec.REASON_SURPLUS),
        ({"conditions": {"requireForeignerExplicit": True}}, rec.REASON_STAY),
        (
            {"conditions": {"requireForeignerExplicit": True, "productType": "DEPOSIT"}},
            rec.REASON_NONE,
        ),
    ],
)
def test_abstain_reasons(overrides: dict[str, Any], reason: str) -> None:
    data = _ok(**overrides)
    assert data["products"] == [] and data["abstainReason"] == reason


def test_excluded_summary_uses_the_first_failed_condition() -> None:
    # SOL글로벌은 12개월(stayTerm)이면서 외국인 명시 상품이다. DEPOSIT 요청이면 kind가 먼저다.
    data = _ok(conditions={"productType": "DEPOSIT"})
    summary = {e["reason"]: e["count"] for e in data["excludedSummary"]}
    assert summary == {"recommendable": 1, "kind": 8}
    assert [p["kind"] for p in data["products"]] == ["DEPOSIT"] * 4
    data = _ok(
        conditions={"requireForeignerExplicit": True}, profile={**REYES, "stayExpiresOn": None}
    )
    assert data["excludedSummary"] == [
        {"reason": "recommendable", "count": 1},
        {"reason": "requireForeignerExplicit", "count": 11},
    ]


def test_missing_profile_values_are_not_checked_and_null_spare_skips_surplus() -> None:
    data = _ok(profile={}, monthlySpareKrw=None, conditions={"monthlyAmountKrw": 50_000})
    assert data["remainingStayMonths"] is None
    assert data["products"]
    for item in data["products"]:
        assert item["stayFits"] is None
        assert {"visa", "age", "remainingStay"} <= set(item["notChecked"])


def test_same_order_and_products_as_the_chat_tool() -> None:
    profile = {**REYES, "asOf": "2026-11-17"}

    class Static:
        async def fetch(self, user_id: int, request_id: str | None) -> ProductContext:
            return ProductContext(ProductProfile.model_validate(profile), SPARE)

    for conditions in (
        {},
        {"productType": "SAVINGS"},
        {"productType": "DEPOSIT", "maxTermMonths": 6},
    ):
        tool = ProductTool(Static(), user_id=1, request_id="r", timeout=2)
        out = asyncio.run(tool.run(RECOMMEND_SAVINGS_PRODUCTS, dict(conditions)))
        assert out.measurement is not None
        chat = out.measurement["recommended"]
        screen = [p["productId"] for p in _ok(conditions=conditions)["products"]]
        assert screen[:5] == chat
        names = [p["name"] for p in json.loads(out.content)["products"]]
        assert names == [p["nameKo"] for p in _ok(conditions=conditions)["products"]][:5]


class Ranker:
    def __init__(self, order: list[str] | None) -> None:
        self.order = order
        self.queries: list[str] = []

    def rank(self, query: str) -> list[str]:
        self.queries.append(query)
        if self.order is None:
            raise RuntimeError("search down")
        return self.order


def test_order_b_with_query_and_a_without_or_on_failure() -> None:
    ids = [p["productId"] for p in CATALOG]
    ranker = Ranker(list(reversed(ids)))
    data = _screen(_body(queryKo=" 단기 예금 "), ranker=ranker)
    assert data["rankedBy"] == "search" and ranker.queries == ["단기 예금"]
    got = [p["productId"] for p in data["products"]]
    assert got == [pid for pid in reversed(ids) if pid in got]
    assert _screen(_body(), ranker=ranker)["rankedBy"] == "catalog_no_query"
    failed = _screen(_body(queryKo="예금"), ranker=Ranker(None))
    assert failed["rankedBy"] == "catalog_search_failed"
    assert [p["productId"] for p in failed["products"]] == [
        p["productId"] for p in _screen(_body())["products"]
    ]


def test_explicit_foreigner_products_come_first() -> None:
    data = _ok(profile={**REYES, "stayExpiresOn": "2028-11-16"})
    assert data["products"][0]["foreignerEligible"] == "explicit"
    assert all(p["foreignerEligible"] != "explicit" for p in data["products"][1:])


@pytest.mark.parametrize("headers", [{}, {"X-Internal-Token": "wrong" * 8}])
def test_requires_the_internal_token(headers: dict[str, str]) -> None:
    response = _client().post(URL, json=_body(), headers=headers)
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"profile": REYES}, "asOf"),
        ({"asOf": "2026-11-17"}, "profile"),
        (_body(asOf="17/11/2026"), "asOf"),
        (_body(monthlySpareKrw=-1), "monthlySpareKrw"),
        (_body(profile={**REYES, "age": 200}), "profile.age"),
        (_body(profile={**REYES, "birthDate": "1996-01-01"}), "profile.birthDate"),
        (_body(conditions={"productType": "LOAN"}), "conditions.productType"),
        (_body(conditions={"maxTermMonths": 0}), "conditions.maxTermMonths"),
        (_body(conditions={"queryKo": "예금"}), "conditions.queryKo"),
        (_body(queryKo="가" * 101), "queryKo"),
        (_body(userId=1), "userId"),
    ],
)
def test_invalid_body_is_400(body: dict[str, Any], field: str) -> None:
    response = _post(body)
    assert response.status_code == 400
    data = response.json()
    assert data["code"] == "VALIDATION_ERROR"
    assert field in [f["field"] for f in data["details"]["fields"]]

"""챗봇 상품 추천 도구(#65): 인자 검증, 프로필 출처, 조건 필터, LLM 결과, 순서."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any

import httpx
import pytest

from app.chat.product_tools import (
    RECOMMEND_SAVINGS_PRODUCTS,
    FixtureProductContext,
    ProductContext,
    ProductProfile,
    ProductTool,
    SpringProductContext,
    load_catalog,
    spare_from_finance,
)
from app.chat.tools import UNAVAILABLE
from app.integrations.spring_client import SpringInternalClient
from app.products import recommender as rec

pytestmark = pytest.mark.unit

CATALOG = load_catalog()
BY_ID = {p["productId"]: p for p in CATALOG}
NAMES = {p["productId"]: rec.value(p, "nameKo") for p in CATALOG}


def _run(tool: ProductTool, args: dict[str, Any]) -> Any:
    return asyncio.run(tool.run(RECOMMEND_SAVINGS_PRODUCTS, args))


def _tool(source: Any = None, **kwargs: Any) -> ProductTool:
    return ProductTool(
        source or FixtureProductContext(), user_id=1, request_id="r1", timeout=2, **kwargs
    )


class Static:
    def __init__(self, context: ProductContext) -> None:
        self.context = context
        self.seen: list[int] = []

    async def fetch(self, user_id: int, request_id: str | None) -> ProductContext:
        self.seen.append(user_id)
        return self.context


def _context(**profile: Any) -> ProductContext:
    spare = profile.pop("spare", 675_317)
    data = {"visaTypeCode": "E-9", "stayExpiresOn": "2027-10-04", "age": 30, "asOf": "2026-11-16"}
    return ProductContext(ProductProfile.model_validate({**data, **profile}), spare)


def test_recommends_what_the_recommender_keeps_for_p1_savings() -> None:
    out = _run(_tool(), {"productType": "SAVINGS"})
    assert out.status == "SUCCEEDED"
    content = json.loads(out.content)
    profile = {"visa": "E-9", "stayExpiry": "2027-10-04", "age": 30, "spareKrw": 675_317}
    expected = rec.recommend_filtered(
        [p["productId"] for p in CATALOG],
        BY_ID,
        profile,
        {"productType": "SAVINGS"},
        date(2026, 11, 16),
        explicit_first=True,
        with_reason=True,
    )["recommended"]
    assert [p["name"] for p in content["products"]] == [NAMES[pid] for pid in expected]
    assert content["user"] == {"remainingStayMonths": 10, "monthlySpareKrw": 675_317}
    assert content["conditions"] == {"productType": "SAVINGS"}
    assert content["abstainReason"] is None
    # LLM에는 문서 id·금리를 주지 않는다. 출처는 문서 id를 chunkRef로.
    assert "doc-" not in out.content and "rate" not in out.content.lower()
    assert [s["chunkRef"] for s in out.sources] == expected
    assert all(s["type"] == "PRODUCT_DOCUMENT" and s["id"] is None for s in out.sources)
    first = content["products"][0]
    assert {"visa", "age"} <= set(first["notChecked"])
    assert first["evidence"] and all(e["quote"] for e in first["evidence"])
    assert out.measurement["recommended"] == expected
    assert out.measurement["rankedBy"] == "catalog"


@pytest.mark.parametrize(
    ("profile", "args", "reason"),
    [
        ("P4", {"productType": "SAVINGS", "monthlyAmountKrw": 300_000}, rec.REASON_SURPLUS),
        ("P1", {"requireForeignerExplicit": True}, rec.REASON_STAY),
        ("P3", {"productType": "DEPOSIT", "requireForeignerExplicit": True}, rec.REASON_NONE),
    ],
)
def test_abstain_reasons(profile: str, args: dict[str, Any], reason: str) -> None:
    out = _run(_tool(FixtureProductContext(profile)), args)
    content = json.loads(out.content)
    assert content["products"] == [] and content["abstainReason"] == reason
    assert out.sources == []


def test_accrual_is_mapped_to_the_catalog_value() -> None:
    content = json.loads(_run(_tool(), {"productType": "SAVINGS", "accrual": "FIXED"}).content)
    assert content["products"] and all(p["accrual"] == "FIXED" for p in content["products"])


def test_missing_profile_values_are_not_checked_instead_of_failing() -> None:
    source = Static(_context(age=None, stayExpiresOn=None, visaTypeCode=None, spare=None))
    out = _run(_tool(source), {"productType": "DEPOSIT", "monthlyAmountKrw": 900_000})
    assert out.status == "SUCCEEDED"
    content = json.loads(out.content)
    assert content["user"] == {"remainingStayMonths": None, "monthlySpareKrw": None}
    assert content["abstainReason"] != rec.REASON_SURPLUS
    assert len(content["notes"]) == 2
    deposit = json.loads(_run(_tool(source), {"productType": "DEPOSIT"}).content)
    assert deposit["products"]
    assert all("remainingStay" in p["notChecked"] for p in deposit["products"])


@pytest.mark.parametrize(
    "args",
    [
        {"productType": "CHECKING"},
        {"monthlyAmountKrw": 0},
        {"accrual": "자유"},
        {"userId": 2},
        {"maxTermMonths": "three"},
    ],
)
def test_invalid_arguments_are_rejected(args: dict[str, Any]) -> None:
    out = _run(_tool(), args)
    assert out.status == "REJECTED" and out.error_code == "INVALID_ARGUMENTS"


def test_uses_the_bound_user_id_only() -> None:
    source = Static(_context())
    tool = ProductTool(source, user_id=7, request_id=None, timeout=2)
    _run(tool, {"productType": "SAVINGS"})
    assert source.seen == [7]


class Ranker:
    def __init__(self, order: list[str] | Exception) -> None:
        self.order = order
        self.queries: list[str] = []

    def rank(self, query: str) -> list[str]:
        self.queries.append(query)
        if isinstance(self.order, Exception):
            raise self.order
        return self.order


def test_search_order_b_and_fallbacks() -> None:
    savings = [
        p["productId"]
        for p in CATALOG
        if rec.value(p, "kind") == "SAVINGS" and p.get("recommendable", True)
    ]
    reverse = list(reversed(savings))
    tool = _tool(FixtureProductContext("P3"), ranker=Ranker(reverse[:3]))
    out = _run(tool, {"productType": "SAVINGS", "queryKo": "적금 추천"})
    assert out.measurement["rankedBy"] == "search"
    names = [p["name"] for p in json.loads(out.content)["products"]]
    explicit = [
        NAMES[pid] for pid in savings if rec.value(BY_ID[pid], "foreignerEligible") == "explicit"
    ]
    assert names[: len(explicit)] == explicit  # explicit 우선은 그대로
    assert names[len(explicit)] == NAMES[next(p for p in reverse if NAMES[p] not in explicit)]

    no_query = _run(_tool(ranker=Ranker(reverse)), {"productType": "SAVINGS"})
    assert no_query.measurement["rankedBy"] == "catalog_no_query"
    failed = _run(_tool(ranker=Ranker(RuntimeError("x"))), {"queryKo": "적금"})
    assert (
        failed.status == "SUCCEEDED" and failed.measurement["rankedBy"] == "catalog_search_failed"
    )


def _spring(handler: Any) -> SpringInternalClient:
    return SpringInternalClient(
        base_url="http://spring.test",
        api_key="s" * 40,
        client=httpx.Client(base_url="http://spring.test", transport=httpx.MockTransport(handler)),
    )


FINANCE = {
    "userId": 3,
    "latestAnalysis": {"id": 7, "spare": {"amount": 200_000, "currency": "KRW"}},
}


def test_spring_source_calls_profile_and_finance() -> None:
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("X-Request-Id")))
        if request.url.path.endswith("/product-profile"):
            body = {
                "visaTypeCode": "E-9",
                "stayExpiresOn": "2027-10-04",
                "age": 30,
                "asOf": "2026-11-16",
            }
            return httpx.Response(200, json=body)
        return httpx.Response(200, json=FINANCE)

    source = SpringProductContext(_spring(handler), timeout=2)
    tool = ProductTool(source, user_id=3, request_id="rid-1", timeout=2)
    out = _run(tool, {"productType": "SAVINGS", "monthlyAmountKrw": 300_000})
    assert json.loads(out.content)["abstainReason"] == rec.REASON_SURPLUS
    assert sorted(path for path, _ in seen) == [
        "/internal/v1/ai-context/users/3/finance",
        "/internal/v1/ai-context/users/3/product-profile",
    ]
    assert {rid for _, rid in seen} == {"rid-1"}


@pytest.mark.parametrize(
    ("response", "status", "code"),
    [
        (httpx.Response(404, json={"code": "USER_NOT_FOUND"}), "FAILED", "SPRING_HTTP_404"),
        (httpx.Response(200, json={"age": -1, "asOf": "2026-11-16"}), "FAILED", "INVALID_RESPONSE"),
    ],
)
def test_spring_errors_answer_without_data(
    response: httpx.Response, status: str, code: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/product-profile"):
            return response
        return httpx.Response(200, json=FINANCE)

    out = _run(
        ProductTool(
            SpringProductContext(_spring(handler), 2), user_id=1, request_id=None, timeout=2
        ),
        {},
    )
    assert (out.status, out.error_code, out.content) == (status, code, UNAVAILABLE)


def test_spring_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    out = _run(
        ProductTool(
            SpringProductContext(_spring(handler), 2), user_id=1, request_id=None, timeout=2
        ),
        {},
    )
    assert (out.status, out.error_code) == ("TIMEOUT", "SPRING_TIMEOUT")


def test_spare_from_finance() -> None:
    assert spare_from_finance(FINANCE) == 200_000
    assert spare_from_finance({"latestAnalysis": None}) is None


def test_unknown_fixture_profile() -> None:
    with pytest.raises(ValueError):
        FixtureProductContext("P9")

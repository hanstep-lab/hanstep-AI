"""금융상품 추천 화면용 추천(이슈 #84, 계약: specs/products/recommend_api_v1.md).

채팅 도구(app/chat/product_tools.py)와 같은 추천기(`recommend_filtered`)·순서(`order_pool`)·
상품 항목(`product_view`)을 쓴다. 다른 점은 셋뿐이다: 프로필을 Spring이 요청 본문으로 넘긴다,
조건을 통과한 상품을 전부 준다, 제외 상품을 이유별 개수로 센다. 금리·특징은 조건표의 원문 값만
옮긴다(계산하지 않는다). LLM을 부르지 않는다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.chat.product_tools import (
    ProductContext,
    ProductProfile,
    ProductRanker,
    RecommendConditions,
    order_pool,
    product_view,
)
from app.products import recommender as rec


class ScreenProfile(BaseModel):
    """product-profile 계약(specs/chat/product_profile_contract_v1.md)과 같은 이름·null 규칙."""

    model_config = ConfigDict(extra="forbid")

    visaTypeCode: str | None = Field(default=None, max_length=20)  # noqa: N815 - 계약 이름
    age: int | None = Field(default=None, ge=0, le=150)
    stayExpiresOn: date | None = None  # noqa: N815


class ScreenRecommendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asOf: date  # noqa: N815
    profile: ScreenProfile
    monthlySpareKrw: int | None = Field(default=None, ge=0)  # noqa: N815
    conditions: RecommendConditions | None = None
    queryKo: str | None = Field(default=None, max_length=100)  # noqa: N815


def _quoted(product: Mapping[str, Any], field: str) -> tuple[Any, Mapping[str, Any]] | None:
    """조건표 값과 원문 근거. 값이나 근거가 없으면 None(지어내지 않는다)."""
    entry = product.get(field)
    if not isinstance(entry, Mapping) or entry.get("value") is None or not entry.get("source"):
        return None
    return entry, entry["source"]


def rate_fields(product: Mapping[str, Any]) -> dict[str, Any]:
    """baseRate·maxRate·feature: 조건표 원문 값을 그대로 옮긴다."""
    out: dict[str, Any] = {"baseRate": None, "maxRate": None, "feature": None}
    if base := _quoted(product, "baseRate"):
        entry, source = base
        out["baseRate"] = {
            "ratePct": entry["value"],
            "asOf": entry.get("asOf"),
            "quote": source["quote"],
            "page": source["page"],
        }
    if top := _quoted(product, "maxRate"):
        entry, source = top
        out["maxRate"] = {
            "ratePct": entry["value"],
            "quote": source["quote"],
            "page": source["page"],
        }
    if feature := _quoted(product, "feature"):
        _, source = feature
        out["feature"] = {"quote": source["quote"], "page": source["page"]}
    return out


def screen_item(product: Mapping[str, Any], checks: Mapping[str, str]) -> dict[str, Any]:
    """products[] 항목: product_view 값에 상품 id·체류 판정·금리·특징을 더한다(키는 늘 같음)."""
    view = product_view(product, checks)
    stay = checks.get("stayTerm")
    return {
        "productId": product["productId"],
        "nameKo": view["name"],
        "kind": view["type"],
        "termMonths": view["termMonths"],
        "monthlyAmountKrw": view.get("monthlyAmountKrw"),
        "depositAmountKrw": view.get("depositAmountKrw"),
        "accrual": view.get("accrual"),
        "foreignerEligible": view["foreigners"],
        "notChecked": view["notChecked"],
        "stayFits": True if stay == rec.OK else None,
        **rate_fields(product),
        "evidence": view["evidence"],
    }


def excluded_summary(
    excluded: Sequence[str],
    products: Mapping[str, Mapping[str, Any]],
    profile: Mapping[str, Any],
    args: Mapping[str, Any],
    as_of: date,
) -> list[dict[str, Any]]:
    """제외 상품을 처음 걸린 조건 하나로 세어 판정 순서대로 준다."""
    counts: dict[str, int] = {}
    for pid in excluded:
        reason = rec.failed_condition(products[pid], profile, args, as_of)
        if reason is None:  # recommend_filtered와 같은 판정이라 일어나지 않는다
            raise RuntimeError(f"excluded product passes: {pid}")
        counts[reason] = counts.get(reason, 0) + 1
    order = (*rec.CONDITIONS, rec.FOREIGNER_EXPLICIT)
    return [{"reason": name, "count": counts[name]} for name in order if name in counts]


async def recommend_for_screen(
    body: ScreenRecommendRequest,
    catalog: Sequence[Mapping[str, Any]],
    ranker: ProductRanker | None,
    search_seconds: float,
) -> dict[str, Any]:
    products = {p["productId"]: p for p in catalog}
    context = ProductContext(
        ProductProfile(**body.profile.model_dump(), asOf=body.asOf), body.monthlySpareKrw
    )
    profile = context.recommender_profile()
    args = (body.conditions or RecommendConditions()).recommender_args()
    pool, ranked_by = await order_pool(ranker, body.queryKo, list(products), search_seconds)
    result = rec.recommend_filtered(
        pool, products, profile, args, body.asOf,
        explicit_first=True, with_reason=True, k=len(pool),
    )  # fmt: skip
    request = rec.request_from_args(args)
    kept = set(result["recommended"])
    return {
        "asOf": body.asOf.isoformat(),
        "remainingStayMonths": rec.remaining_stay_months(profile, body.asOf),
        "rankedBy": ranked_by,
        "abstainReason": result["reason"],
        "products": [
            screen_item(
                products[pid], rec.check_product(products[pid], profile, request, body.asOf)
            )
            for pid in result["recommended"]
        ],
        "excludedSummary": excluded_summary(
            [pid for pid in pool if pid not in kept], products, profile, args, body.asOf
        ),
    }

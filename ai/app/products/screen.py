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
    excluded_summary,
    order_pool,
    rate_fields,
    screen_item,
)

# rate_fields·screen_item·excluded_summary는 채팅 카드(PRODUCT_RECOMMEND_CARD, #114)와 같이 쓰려고
# app/chat/product_tools.py로 옮겼다(이 모듈이 product_tools를 가져다 쓰므로 반대로는 순환 import).
__all__ = [
    "ScreenRecommendRequest",
    "excluded_summary",
    "rate_fields",
    "recommend_for_screen",
    "screen_item",
]
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

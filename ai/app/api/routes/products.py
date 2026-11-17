"""``POST /internal/v1/products/recommend``: 금융상품 추천 화면(FP-001)용 추천(이슈 #84).

계약: specs/products/recommend_api_v1.md.
채팅 상품 추천 도구와 같은 추천기를 쓰고 LLM은 부르지 않는다.
Spring이 프로필·여유자금을 본문으로 넘기고, 결과를 FE 응답 모양으로 조립한다.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.chat.product_tools import load_catalog
from app.products.screen import ScreenRecommendRequest, recommend_for_screen

# app.api.routes.internal이 /internal/v1 아래에 붙이고, 거기서 내부 토큰을 검사한다.
router = APIRouter(prefix="/products", tags=["products"])


@router.post("/recommend")
async def recommend_products(body: ScreenRecommendRequest, request: Request) -> dict[str, Any]:
    state = request.app.state.rag
    # 순서 B(상품설명서 검색)는 채팅 도구와 같은 검색기·시간 예산을 쓴다. 없거나 실패하면 순서 A.
    return await recommend_for_screen(
        body,
        load_catalog(),
        state.product_search,
        state.settings.chat_tool_timeout_seconds,
    )

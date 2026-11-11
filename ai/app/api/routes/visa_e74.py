"""``POST /internal/v1/visa/e74/calculate`` (Spring Boot -> FastAPI, 10단계).

Spring이 보낸 프로필 스냅샷으로 E-7-4 점수를 계산한다. LLM·DB를 쓰지 않고 저장도 하지 않는다.
결과 누적과 재사용(requestKey), 사용자별 접근권한 확인은 Spring이 한다.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.errors import ApiError
from app.core.config import Settings
from app.visa.e74.schemas import E74CalculateRequest
from app.visa.e74.service import E74CalculateResponse, E74ScenarioError, calculate

# app.api.routes.internal이 /internal/v1 아래에 붙이고 내부 토큰을 검사한다.
router = APIRouter(prefix="/visa/e74", tags=["visa-e74"])


@router.post("/calculate", response_model=E74CalculateResponse, response_model_by_alias=True)
def calculate_e74_score(body: E74CalculateRequest, request: Request) -> E74CalculateResponse:
    # 동기 def: 수 ms짜리 CPU 계산이라 FastAPI 스레드풀에서 돈다.
    settings: Settings = request.app.state.rag.settings
    try:
        return calculate(body, include_draft=settings.e74_include_draft_rules)
    except E74ScenarioError as exc:
        raise ApiError(400, "VALIDATION_ERROR", "invalid scenario", {"fields": exc.fields}) from exc

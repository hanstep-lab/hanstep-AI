"""``POST /internal/v1/visa/roadmap`` (Spring Boot -> FastAPI).

Spring이 보낸 프로필 스냅샷과 체류 만료일로 E-7-4 준비 단계를 만든다. DB에 접근하지 않고
저장도 하지 않는다. 결과 저장(visa_roadmaps)과 사용자별 접근권한 확인은 Spring이 한다.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.core.config import Settings
from app.visa.roadmap.schemas import RoadmapRequest, RoadmapResponse
from app.visa.roadmap.service import build_roadmap

# app.api.routes.internal이 /internal/v1 아래에 붙이고 내부 토큰을 검사한다.
router = APIRouter(prefix="/visa", tags=["visa-roadmap"])


@router.post("/roadmap", response_model=RoadmapResponse, response_model_by_alias=True)
def create_visa_roadmap(body: RoadmapRequest, request: Request) -> RoadmapResponse:
    # 동기 def: 엔진 계산과 (켜져 있으면) LLM 호출이 FastAPI 스레드풀에서 돈다.
    state = request.app.state.rag
    settings: Settings = state.settings
    return build_roadmap(
        body, include_draft=settings.e74_include_draft_rules, writer=state.roadmap_writer
    )

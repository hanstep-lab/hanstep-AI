"""``/internal/v1``: BE -> AI 내부 API. 여기의 모든 경로는 내부 토큰이 필요하다."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.routes import finance, visa_e74, visa_policies
from app.api.security import require_internal_token

router = APIRouter(prefix="/internal/v1", dependencies=[Depends(require_internal_token)])
router.include_router(visa_policies.router)
router.include_router(visa_e74.router)
router.include_router(finance.router)

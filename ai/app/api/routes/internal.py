"""``/internal/v1``: BE -> AI internal API. Every route here requires the internal token."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.routes import visa_policies
from app.api.security import require_internal_token

router = APIRouter(prefix="/internal/v1", dependencies=[Depends(require_internal_token)])
router.include_router(visa_policies.router)

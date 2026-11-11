"""``POST /internal/v1/finance/analyze`` (INT-01, Spring Boot -> FastAPI).

Rule-based monthly balance from Spring's stored transactions. No LLM is involved.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.errors import ApiError
from app.finance.calculator import analyze
from app.finance.normalizer import FinanceValidationError
from app.finance.schemas import FinanceAnalyzeRequest, FinanceAnalyzeResponse

# Mounted under /internal/v1 by app.api.routes.internal, which enforces the internal token.
router = APIRouter(prefix="/finance", tags=["finance"])


@router.post("/analyze", response_model=FinanceAnalyzeResponse, response_model_by_alias=True)
def analyze_transactions(body: FinanceAnalyzeRequest) -> FinanceAnalyzeResponse:
    # Sync def: CPU-bound pandas work runs in FastAPI's threadpool.
    try:
        return analyze(body)
    except FinanceValidationError as exc:
        fields = [{"field": e.field, "reason": e.reason} for e in exc.errors]
        raise ApiError(400, "VALIDATION_ERROR", "invalid request", {"fields": fields}) from exc

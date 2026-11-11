"""``POST /internal/v1/finance/analyze``(INT-01, Spring Boot -> FastAPI).

Spring이 저장한 거래 내역으로 규칙 기반 월별 수지를 계산한다. LLM은 쓰지 않는다.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.errors import ApiError
from app.finance.calculator import analyze
from app.finance.normalizer import FinanceValidationError
from app.finance.schemas import FinanceAnalyzeRequest, FinanceAnalyzeResponse

# app.api.routes.internal이 /internal/v1 아래에 붙이고, 거기서 내부 토큰을 검사한다.
router = APIRouter(prefix="/finance", tags=["finance"])


@router.post("/analyze", response_model=FinanceAnalyzeResponse, response_model_by_alias=True)
def analyze_transactions(body: FinanceAnalyzeRequest) -> FinanceAnalyzeResponse:
    # 동기 def로 둔다. CPU를 많이 쓰는 pandas 작업이 FastAPI 스레드 풀에서 실행된다.
    try:
        return analyze(body)
    except FinanceValidationError as exc:
        fields = [{"field": e.field, "reason": e.reason} for e in exc.errors]
        raise ApiError(400, "VALIDATION_ERROR", "invalid request", {"fields": fields}) from exc

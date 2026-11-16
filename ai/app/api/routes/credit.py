"""``POST /internal/v1/credit/alt-assessments`` (Spring Boot -> FastAPI).

외국인 대안신용 모의 사전진단(모델 V9). Spring이 모아 보낸 값으로 계산만 한다. LLM·DB를 쓰지 않고
저장도 하지 않는다. 결과는 실제 대출 승인이나 신용점수가 아니다(응답 disclaimer).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.credit.alt_score import AltCreditAssessment, assess
from app.credit.schemas import AltCreditRequest

# app.api.routes.internal이 /internal/v1 아래에 붙이고 내부 토큰을 검사한다.
router = APIRouter(prefix="/credit", tags=["credit"])


@router.post("/alt-assessments", response_model=AltCreditAssessment, response_model_by_alias=True)
def create_alt_assessment(body: AltCreditRequest) -> AltCreditAssessment:
    # 동기 def: 1 ms 미만의 CPU 계산이라 FastAPI 스레드풀에서 돈다.
    return assess(body.to_input(), is_mock=body.is_mock)

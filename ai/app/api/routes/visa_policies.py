"""``POST /internal/v1/visa/policies/search``(API 명세 v1.6, Spring Boot -> FastAPI).

승인된 공식 정책 청크를 출처, 적용 기간과 함께 돌려준다. 검색은 적재나 승인을 하지 않는다.
정책 적재는 RabbitMQ(ai_jobs POLICY_INGEST)로 들어온다.
"""

from __future__ import annotations

import asyncio
from datetime import date

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.api.errors import ApiError
from app.rag.errors import ValidationFailedError
from app.rag.retrieval import SearchHit, SearchService, SearchUnavailableError

# app.api.routes.internal이 /internal/v1 아래에 붙이고, 거기서 내부 토큰을 검사한다.
router = APIRouter(prefix="/visa/policies", tags=["visa-policies"])
SEARCH_TIMEOUT_S = 20.0


class CamelRequest(BaseModel):
    """camelCase JSON 요청 모델의 공통 기반."""

    # JSON은 camelCase다(명세 §0.1). snake_case 이름은 입력에서 거부한다.
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=False, extra="forbid")


class CamelResponse(BaseModel):
    """camelCase JSON 응답 모델의 공통 기반."""

    # Python 이름으로 만들고 camelCase로 직렬화한다.
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class PolicySearchRequest(CamelRequest):
    """정책 검색 요청 본문. 비자 유형으로 범위를 좁힐 수 있다."""

    query: str = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=5, ge=1, le=20)
    visa_types: list[str] | None = Field(default=None, max_length=10)


class PolicySearchResult(CamelResponse):
    """검색된 정책 청크 한 건. 출처와 적용 기간을 함께 담는다."""

    chunk_id: str
    policy_document_id: int
    document_version: str
    chunk_index: int
    text: str
    title: str
    source_url: str
    source_org: str
    source_type: str
    visa_types: list[str]
    score: float
    page_number: int | None = None
    section_title: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    issued_on: date | None = None

    @classmethod
    def from_hit(cls, hit: SearchHit) -> PolicySearchResult:
        return cls(
            chunk_id=hit.chunk_id,
            policy_document_id=hit.policy_document_id,
            document_version=hit.document_version,
            chunk_index=hit.chunk_index,
            text=hit.chunk_text,
            title=hit.title,
            source_url=hit.source_url,
            source_org=hit.source_org,
            source_type=hit.source_type,
            visa_types=hit.visa_types,
            score=hit.score,
            page_number=hit.page_number,
            section_title=hit.section_title,
            effective_from=hit.effective_from,
            effective_to=hit.effective_to,
            issued_on=hit.issued_on,
        )


class PolicySearchResponse(CamelResponse):
    """정책 검색 응답."""

    results: list[PolicySearchResult]


@router.post("/search", response_model=PolicySearchResponse, response_model_by_alias=True)
async def search(body: PolicySearchRequest, request: Request) -> PolicySearchResponse:
    state = request.app.state.rag
    if state.mongo is None:
        raise ApiError(503, "SERVICE_UNAVAILABLE", "vector store not configured")
    if not state.embedder.loaded:
        # 요청 안에서 가중치를 불러오거나 내려받으면 타임아웃을 넘긴다.
        raise ApiError(503, "SERVICE_UNAVAILABLE", "embedding model not loaded")
    settings = state.settings
    service = SearchService(
        state.mongo[settings.mongo_database][settings.mongo_collection],
        state.embedder,
        state.gateway,
        index_name=settings.mongo_vector_index,
        num_candidates=settings.search_num_candidates,
    )
    try:
        hits = await asyncio.wait_for(
            run_in_threadpool(service.search, body.query, body.top_k, body.visa_types),
            timeout=SEARCH_TIMEOUT_S,
        )
    except ValidationFailedError as exc:
        raise ApiError(400, "VALIDATION_ERROR", str(exc)) from exc
    except SearchUnavailableError as exc:
        # fail-closed: 승인 상태를 확인하지 못했다.
        raise ApiError(503, "SERVICE_UNAVAILABLE", "approval state unavailable") from exc
    except TimeoutError as exc:
        raise ApiError(504, "UPSTREAM_TIMEOUT", "search timed out") from exc
    return PolicySearchResponse(results=[PolicySearchResult.from_hit(hit) for hit in hits])

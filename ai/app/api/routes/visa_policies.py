"""``POST /internal/v1/visa/policies/search`` (API spec v1.6, Spring Boot -> FastAPI).

Returns approved official policy chunks with source and effective period. Search never runs
ingestion or approval; policy ingestion arrives through RabbitMQ (ai_jobs POLICY_INGEST).
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

# Mounted under /internal/v1 by app.api.routes.internal, which enforces the internal token.
router = APIRouter(prefix="/visa/policies", tags=["visa-policies"])
SEARCH_TIMEOUT_S = 20.0


class CamelRequest(BaseModel):
    # JSON is camelCase (spec §0.1); snake_case names are rejected on input.
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=False, extra="forbid")


class CamelResponse(BaseModel):
    # Built from Python names, serialized as camelCase.
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class PolicySearchRequest(CamelRequest):
    query: str = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=5, ge=1, le=20)
    visa_types: list[str] | None = Field(default=None, max_length=10)


class PolicySearchResult(CamelResponse):
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
    results: list[PolicySearchResult]


@router.post("/search", response_model=PolicySearchResponse, response_model_by_alias=True)
async def search(body: PolicySearchRequest, request: Request) -> PolicySearchResponse:
    state = request.app.state.rag
    if state.mongo is None:
        raise ApiError(503, "SERVICE_UNAVAILABLE", "vector store not configured")
    if not state.embedder.loaded:
        # Loading/downloading weights inside a request would blow the timeout.
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
        # Fail closed: approval state could not be verified.
        raise ApiError(503, "SERVICE_UNAVAILABLE", "approval state unavailable") from exc
    except TimeoutError as exc:
        raise ApiError(504, "UPSTREAM_TIMEOUT", "search timed out") from exc
    return PolicySearchResponse(results=[PolicySearchResult.from_hit(hit) for hit in hits])

"""Internal RAG endpoints.

Paths and DTOs are proposals until the backend contract is confirmed
(specs/rag/api_contracts.md). Search never runs ingestion or approval.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from app.integrations.policy_gateway import MISSING_JOB_CONTRACT
from app.rag.errors import ValidationFailedError
from app.rag.retrieval import SearchService, SearchUnavailableError

# Mounted under /internal/v1 by app.api.routes.internal, which enforces the internal token.
router = APIRouter(prefix="/rag", tags=["rag"])
SEARCH_TIMEOUT_S = 20.0


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=5, ge=1, le=20)
    visa_types: list[str] | None = Field(default=None, max_length=10)
    domain: str = "visa"


class SearchHitDto(BaseModel):
    policy_document_id: int
    document_version: str
    chunk_index: int
    chunk_text: str
    title: str
    source_url: str
    source_org: str
    visa_types: list[str]
    score: float
    page_number: int | None = None
    section_title: str | None = None


class SearchResponse(BaseModel):
    results: list[SearchHitDto]


@router.post("/search", response_model=SearchResponse)
async def search(body: SearchRequest, request: Request) -> SearchResponse:
    state = request.app.state.rag
    if body.domain != "visa":
        raise HTTPException(status_code=501, detail="finance search contract not confirmed")
    if state.mongo is None:
        raise HTTPException(status_code=503, detail="vector store not configured")
    if not state.embedder.loaded:
        # Loading/downloading weights inside a request would blow the timeout.
        raise HTTPException(status_code=503, detail="embedding model not loaded")
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
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SearchUnavailableError as exc:
        # Fail closed: approval state could not be verified.
        raise HTTPException(status_code=503, detail="approval state unavailable") from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="search timed out") from exc
    return SearchResponse(results=[SearchHitDto(**asdict(hit)) for hit in hits])


class IngestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=8, max_length=128)
    manifest_paths: list[str] = Field(default_factory=list, max_length=100)
    domain: str = "visa"


@router.post("/ingestions", status_code=202)
async def create_ingestion(body: IngestionRequest, request: Request) -> dict[str, str]:
    # Accepting without a persisted ai_jobs row and guaranteed queue delivery would report
    # work that may never run, so this stays closed until the contract exists.
    raise HTTPException(status_code=503, detail=MISSING_JOB_CONTRACT)


@router.get("/ingestions/{job_id}")
async def get_ingestion(job_id: str, request: Request) -> dict[str, str]:
    raise HTTPException(status_code=503, detail=MISSING_JOB_CONTRACT)

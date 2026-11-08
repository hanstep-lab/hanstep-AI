"""Liveness and readiness. Readiness reports component state without secrets."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app.rag.indexes import inspect_vector_index, load_definition

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


def _check_mongo(state: Any) -> dict[str, str]:
    if state.mongo is None:
        return {"mongodb": "not_configured", "vector_index": "unknown"}
    try:
        state.mongo.admin.command("ping")
    except Exception as exc:
        return {"mongodb": f"error:{type(exc).__name__}", "vector_index": "unknown"}
    settings = state.settings
    collection = state.mongo[settings.mongo_database][settings.mongo_collection]
    try:
        status = inspect_vector_index(collection, settings.mongo_vector_index, load_definition())
        index = f"{status.state}:{'queryable' if status.queryable else 'not_queryable'}"
    except Exception as exc:
        index = f"error:{type(exc).__name__}"
    return {"mongodb": "ok", "vector_index": index}


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    state = request.app.state.rag
    checks = await run_in_threadpool(_check_mongo, state)
    checks["model"] = "loaded" if state.embedder.loaded else state.errors.get("model", "not_loaded")
    checks["policy_gateway"] = "configured" if state.settings.spring_configured() else "blocked"
    ok = (
        checks["mongodb"] == "ok"
        and checks["vector_index"] == "match:queryable"
        and checks["model"] == "loaded"
        and checks["policy_gateway"] == "configured"
    )
    return JSONResponse({"ready": ok, "checks": checks}, status_code=200 if ok else 503)

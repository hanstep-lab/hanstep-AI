"""생존(liveness)과 준비(readiness) 확인. 준비 확인은 비밀값 없이 구성 요소 상태를 알린다."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app.integrations.policy_gateway import UnconfiguredPolicyGateway
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


def _search_check(status: str, errors: dict[str, str], key: str) -> str:
    """not_configured · building · ready · error:<예외 타입>. 예외 메시지는 내보내지 않는다."""
    if status == "error":
        return f"error:{errors.get(key, 'unknown')}"
    return status


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    state = request.app.state.rag
    checks = await run_in_threadpool(_check_mongo, state)
    if state.embedder.loaded:
        checks["model"] = "loaded"
    elif "model" in state.errors:
        checks["model"] = f"error:{state.errors['model']}"
    else:
        checks["model"] = getattr(state, "model_status", "not_loaded")
    # 검색은 게이트웨이로 승인을 확인한다. 게이트웨이가 미설정이면 모든 검색이 503이 된다.
    unconfigured = isinstance(state.gateway, UnconfiguredPolicyGateway)
    checks["policy_gateway"] = "blocked" if unconfigured else "configured"
    checks["internal_auth"] = (
        "configured" if state.settings.internal_token_configured() else "not_configured"
    )
    # 검색기 구축 상태(#98). 모델 로드 뒤에 만들어지므로 모델만 보면 구축 중에도 ready가 true였다.
    checks["productSearch"] = _search_check(
        state.product_search_status, state.errors, "productSearch"
    )
    checks["financePolicySearch"] = _search_check(
        state.finance_policy_search_status, state.errors, "financePolicySearch"
    )
    ok = (
        checks["mongodb"] == "ok"
        and checks["vector_index"] == "match:queryable"
        and checks["model"] == "loaded"
        and checks["policy_gateway"] == "configured"
        and checks["internal_auth"] == "configured"
        # 문서 폴더가 설정된 검색기가 아직 building이면 준비되지 않았다.
        and state.product_search_status != "building"
        # 금융 정책 검색기는 실패하면 대신할 경로가 없어(도구 FAILED) error도 막는다. 상품 검색기는
        # 실패해도 도구가 조건표 순서로 대신하므로 error는 checks에만 보인다.
        and state.finance_policy_search_status in ("not_configured", "ready")
    )
    return JSONResponse({"ready": ok, "checks": checks}, status_code=200 if ok else 503)

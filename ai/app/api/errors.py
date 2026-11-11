"""팀 공통 오류 응답 형식: ``{code, message, details, requestId}``(API 명세 v1.6 §0.2)."""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-Id"
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_\-:.]{1,64}$")

# 명세 코드에 AI 쪽 사용 불가를 뜻하는 503/504를 더했다(Spring은 이를 AI_UPSTREAM_ERROR로 바꾼다).
STATUS_CODES = {
    400: "VALIDATION_ERROR",
    401: "UNAUTHORIZED",
    404: "RESOURCE_NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "INVALID_STATE",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    503: "SERVICE_UNAVAILABLE",
    504: "UPSTREAM_TIMEOUT",
}


class ApiError(Exception):
    """팀 공통 오류 응답으로 바뀌는 API 예외(상태, 코드, 메시지, 상세)."""

    def __init__(
        self, status: int, code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


def request_id(request: Request) -> str:
    """호출자의 요청 ID(Spring 연계 추적용)를 재사용하거나 새로 만든다."""
    provided = request.headers.get(REQUEST_ID_HEADER, "")
    if _SAFE_REQUEST_ID.match(provided):
        return provided
    return f"req_{uuid.uuid4().hex[:20]}"


def _envelope(
    request: Request, status: int, code: str, message: str, details: dict[str, Any]
) -> JSONResponse:
    rid = request_id(request)
    return JSONResponse(
        {"code": code, "message": message, "details": details, "requestId": rid},
        status_code=status,
        headers={REQUEST_ID_HEADER: rid},
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _envelope(request, exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = STATUS_CODES.get(exc.status_code, "INTERNAL_ERROR")
        message = exc.detail if isinstance(exc.detail, str) else code.lower()
        return _envelope(request, exc.status_code, code, message, {})

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # 필드 경로와 사유만 담는다. 제출한 값은 절대 되돌려 보내지 않는다(질의에 개인정보가
        # 있을 수 있음).
        fields = [
            {"field": ".".join(str(p) for p in err["loc"] if p != "body"), "reason": err["type"]}
            for err in exc.errors()
        ]
        return _envelope(request, 400, "VALIDATION_ERROR", "invalid request", {"fields": fields})

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error")
        return _envelope(request, 500, "INTERNAL_ERROR", "internal error", {})

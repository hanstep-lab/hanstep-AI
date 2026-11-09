"""Team-wide error envelope: ``{code, message, details, requestId}`` (API spec v1.6 §0.2)."""

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

# Spec codes, plus 503/504 for AI-side unavailability (Spring maps these to AI_UPSTREAM_ERROR).
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
    def __init__(
        self, status: int, code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


def request_id(request: Request) -> str:
    """Reuse the caller's request id (Spring correlation) or mint one."""
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
        # Field paths and reasons only; never echo submitted values (queries may hold PII).
        fields = [
            {"field": ".".join(str(p) for p in err["loc"] if p != "body"), "reason": err["type"]}
            for err in exc.errors()
        ]
        return _envelope(request, 400, "VALIDATION_ERROR", "invalid request", {"fields": fields})

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error")
        return _envelope(request, 500, "INTERNAL_ERROR", "internal error", {})

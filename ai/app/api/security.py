"""Shared-token authentication for BE -> AI internal calls."""

from __future__ import annotations

import hmac
import logging

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)


def require_internal_token(request: Request) -> None:
    """Reject unless the configured header carries ``AI_INTERNAL_API_TOKEN``.

    Fails closed: with no (or a too-short/placeholder) token configured, every internal call
    is rejected. Comparison is constant-time; the token is never logged.
    """
    settings = request.app.state.rag.settings
    if not settings.internal_token_configured():
        logger.warning("AI_INTERNAL_API_TOKEN is not configured; rejecting internal call")
        raise HTTPException(status_code=401, detail="unauthorized")
    provided = request.headers.get(settings.internal_token_header, "")
    expected = settings.ai_internal_api_token.get_secret_value()
    if not provided or not hmac.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="unauthorized")

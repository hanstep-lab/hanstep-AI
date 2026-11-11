"""BE -> AI 내부 호출용 공유 토큰 인증."""

from __future__ import annotations

import hmac
import logging

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)


def require_internal_token(request: Request) -> None:
    """설정한 헤더에 ``AI_INTERNAL_API_TOKEN`` 값이 없으면 거부한다.

    fail-closed: 설정된 토큰이 없거나 너무 짧거나 예시값이면 모든 내부 호출을 거부한다.
    비교는 상수 시간으로 하고, 토큰은 절대 로그에 남기지 않는다.
    """
    settings = request.app.state.rag.settings
    if not settings.internal_token_configured():
        logger.warning("AI_INTERNAL_API_TOKEN is not configured; rejecting internal call")
        raise HTTPException(status_code=401, detail="unauthorized")
    provided = request.headers.get(settings.internal_token_header, "")
    expected = settings.ai_internal_api_token.get_secret_value()
    if not provided or not hmac.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="unauthorized")

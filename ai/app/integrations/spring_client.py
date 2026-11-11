"""AI/Worker -> Spring 내부 API 클라이언트(BE명세서 INT-08, INT-10, INT-13).

경로는 BE 명세 초안을 따른다. 요청 본문은 AI 쪽에서 정하고(x-role: 정하기) 그 명세에
다시 반영한다. 모든 보고는 반복해도 안전하다. Spring은 중복 요청에 같은 2xx로 답해야 하므로
전송 오류와 5xx는 정해진 횟수까지만 재시도한다.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import httpx

from app.rag.errors import ContractBlockedError

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# 정책 적재 워커가 INT-10으로 보고하는 실패 코드.
PolicyFailureCode = Literal[
    "SOURCE_FETCH_FAILED",  # R2/제공처에서 원본을 내려받지 못함(재시도 가능)
    "HASH_MISMATCH",  # 바이트가 MySQL content_hash와 다름
    "EXTRACTION_FAILED",  # 텍스트를 추출하지 못함
    "NEEDS_OCR",  # 스캔 PDF
    "NEEDS_CONVERSION",  # 직접 지원하지 않는 형식
    "VERSION_CONFLICT",  # 같은 버전이 다른 해시/다른 청크로 저장되어 있음
    "EMBEDDING_FAILED",  # 모델 오류(재시도 가능)
    "VECTOR_STORE_FAILED",  # MongoDB 쓰기/검증 실패(재시도 가능)
]
RETRYABLE_FAILURES: frozenset[str] = frozenset(
    {"SOURCE_FETCH_FAILED", "EMBEDDING_FAILED", "VECTOR_STORE_FAILED"}
)


class SpringApiError(Exception):
    """Spring 내부 API 호출 실패. HTTP 상태와 Spring 오류 코드를 담는다."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"Spring {status} {code}: {message}")
        self.status = status
        self.code = code


@dataclass(frozen=True)
class PolicyIngestedReport:
    """INT-13 본문. 필드 이름은 policy_documents 컬럼에 대응한다(명세 문서 참고)."""

    document_version: str
    content_hash: str
    chunk_count: int
    mongo_collection: str
    embedding_model: str
    embedding_revision: str
    object_key: str | None
    ingested_at: datetime

    def to_json(self) -> dict[str, Any]:
        return {
            "documentVersion": self.document_version,
            "contentHash": self.content_hash,
            "chunkCount": self.chunk_count,
            "mongoCollection": self.mongo_collection,
            "embeddingModel": self.embedding_model,
            "embeddingRevision": self.embedding_revision,
            "objectKey": self.object_key,
            "ingestedAt": self.ingested_at.isoformat(),
        }


class SpringInternalClient:
    """작업 시작·실패(INT-08, INT-10)와 정책 적재 완료(INT-13)를 Spring에 보고한다."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        header: str = "X-Internal-Token",
        client: httpx.Client | None = None,
        max_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not base_url or not api_key:
            raise ContractBlockedError(
                "SPRING_INTERNAL_API_URL / SPRING_INTERNAL_API_KEY not configured"
            )
        self._headers = {header: api_key}
        self._client = client or httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(connect=3.0, read=10.0, write=10.0, pool=3.0),
            follow_redirects=False,
        )
        self._max_attempts = max_attempts
        self._sleep = sleep

    def close(self) -> None:
        self._client.close()

    def _post(self, path: str, body: dict[str, Any], request_id: str | None) -> None:
        headers = dict(self._headers)
        if request_id:
            headers["X-Request-Id"] = request_id
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._client.post(path, json=body, headers=headers)
            except httpx.TransportError as exc:
                if attempt == self._max_attempts:
                    raise SpringApiError(0, "TRANSPORT_ERROR", type(exc).__name__) from exc
                self._sleep(min(2**attempt, 10))
                continue
            if response.is_success:
                return
            if response.status_code in RETRYABLE_STATUS and attempt < self._max_attempts:
                retry_after = response.headers.get("Retry-After", "")
                self._sleep(float(retry_after) if retry_after.isdigit() else min(2**attempt, 10))
                continue
            code, message = "HTTP_ERROR", ""
            try:
                payload = response.json()
                code, message = str(payload.get("code", code)), str(payload.get("message", ""))
            except ValueError:
                pass
            raise SpringApiError(response.status_code, code, message)
        raise SpringApiError(0, "RETRY_EXHAUSTED", path)  # pragma: no cover

    def _get(self, path: str, request_id: str | None, timeout: float) -> Any:
        """One GET, no retry: callers (chat tools) have their own short time budget."""
        headers = dict(self._headers)
        if request_id:
            headers["X-Request-Id"] = request_id
        response = self._client.get(path, headers=headers, timeout=timeout)
        if response.is_success:
            return response.json()
        code = "HTTP_ERROR"
        try:
            code = str(response.json().get("code", code))
        except ValueError:
            pass
        raise SpringApiError(response.status_code, code, "")

    # INT-06. httpx.TimeoutException on timeout, SpringApiError on non-2xx.
    def finance_context(
        self, user_id: int, *, timeout: float, request_id: str | None = None
    ) -> dict[str, Any]:
        body = self._get(f"/internal/v1/ai-context/users/{user_id}/finance", request_id, timeout)
        if not isinstance(body, dict):
            raise SpringApiError(200, "INVALID_BODY", "")
        return body

    # INT-08
    def job_started(
        self, job_id: int, *, worker_id: str, started_at: datetime, request_id: str | None = None
    ) -> None:
        body = {"workerId": worker_id, "startedAt": started_at.isoformat()}
        try:
            self._post(f"/internal/v1/ai/jobs/{job_id}/started", body, request_id)
        except SpringApiError as exc:
            # 이미 시작했거나 끝난 작업의 메시지가 다시 전달된 것은 여기서 오류가 아니다.
            if exc.status == 409:
                logger.info("job %s already started or finished", job_id)
                return
            raise

    # INT-10
    def job_failed(
        self,
        job_id: int,
        *,
        error_code: PolicyFailureCode,
        message: str,
        request_id: str | None = None,
    ) -> None:
        body = {
            "errorCode": error_code,
            # 요약만 보낸다. 개인정보와 원문은 넣지 않는다(API 명세 0.3).
            "message": message[:500],
            "retryable": error_code in RETRYABLE_FAILURES,
        }
        self._post(f"/internal/v1/ai/jobs/{job_id}/failed", body, request_id)

    # INT-13
    def policy_ingested(
        self,
        policy_document_id: int,
        report: PolicyIngestedReport,
        *,
        request_id: str | None = None,
    ) -> None:
        self._post(
            f"/internal/v1/ai/policies/{policy_document_id}/ingested", report.to_json(), request_id
        )

"""OCR Worker -> Spring 내부 API 클라이언트.

- INT-08 ``POST /internal/v1/ai/jobs/{jobId}/started``
- INT-10 ``POST /internal/v1/ai/jobs/{jobId}/failed``
- ``GET /internal/v1/ai-context/documents/{documentId}/source`` (hanstep-backend #81)
- ``POST /internal/v1/ai/documents/{documentId}/ocr-result`` (hanstep-backend #81)

재시도 책임: started·source·ocr-result는 한 번만 부르고 일시 오류는 Worker가 retry 큐로 다시
시도한다. started를 클라이언트에서 다시 보내면 첫 요청이 반영된 경우 409 JOB_ALREADY_PROCESSING을
받아 중복 메시지로 보고 ack하게 된다(작업이 PROCESSING에 남는다). failed는 마지막 보고라 retry
큐가 없으므로 여기서만 정해진 횟수까지 다시 보낸다.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from enum import Enum
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# OCR Worker가 INT-10으로 보고하는 실패 코드. 문서 조회(DOC-02)의 failureCode로 사용자에게 간다.
OcrFailureCode = Literal[
    "SOURCE_MISSING",  # 문서에 R2 원본 키가 없거나 그 객체가 없음
    "SOURCE_FETCH_FAILED",  # R2 연결 실패·시간 초과·5xx(재시도 가능)
    "HASH_MISMATCH",  # 받은 바이트가 documents.sha256과 다름
    "INVALID_IMAGE",  # 크기 제한 초과 등 엔진에 넣을 수 없는 원본
    "UNSUPPORTED_SIDE",  # 앞면(FRONT)이 아닌 문서
    # 엔진 오류, 또는 필수 항목(nameEn·nationality·visaType·registrationNo)을 모두 못 읽음
    "OCR_FAILED",
    "SPRING_UNAVAILABLE",  # Spring 내부 API 연결 실패·시간 초과·5xx·429(재시도 가능)
    "SPRING_REJECTED",  # Spring 내부 API 4xx(문서 없음, 상태 충돌 등)
    "WORKER_ERROR",  # 분류하지 않은 Worker 내부 오류
]
RETRYABLE_FAILURES: frozenset[str] = frozenset({"SOURCE_FETCH_FAILED", "SPRING_UNAVAILABLE"})


class StartResult(str, Enum):
    """INT-08 결과. Worker는 이 값과 메시지 redelivered·x-retry-count로 처리할지 정한다."""

    STARTED = "STARTED"  # 204: 이번 요청으로 PROCESSING이 됨
    ALREADY_PROCESSING = "ALREADY_PROCESSING"  # 409 JOB_ALREADY_PROCESSING: 이미 처리 중
    ALREADY_FINISHED = "ALREADY_FINISHED"  # 409 JOB_ALREADY_FINISHED: 이미 COMPLETED·FAILED


# INT-08 409의 body.code별 결과. 여기에 없는 code의 409는 SpringApiError로 올린다.
_START_CONFLICTS: dict[str, StartResult] = {
    "JOB_ALREADY_PROCESSING": StartResult.ALREADY_PROCESSING,
    "JOB_ALREADY_FINISHED": StartResult.ALREADY_FINISHED,
}


class SpringApiError(Exception):
    """Spring 내부 API 호출 실패. HTTP 상태와 Spring 오류 코드만 담는다(본문 값은 싣지 않는다).

    status 0은 연결 실패·시간 초과다.
    """

    def __init__(self, status: int, code: str) -> None:
        super().__init__(f"Spring {status} {code}")
        self.status = status
        self.code = code


class DocumentSource(BaseModel):
    """OCR 원본 위치(DocumentSourceResponse). 개인정보는 없다."""

    model_config = ConfigDict(alias_generator=to_camel, frozen=True, extra="ignore")

    # R2 ARC 버킷 키. 원본이 삭제됐으면 null.
    object_key: str | None = None
    side: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SpringInternalClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        header: str = "X-Internal-Token",
        client: httpx.Client | None = None,
        failed_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not base_url or not api_key:
            raise ValueError("SPRING_INTERNAL_API_URL / SPRING_INTERNAL_API_KEY not configured")
        self._headers = {header: api_key}
        self._client = client or httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(connect=3.0, read=10.0, write=10.0, pool=3.0),
            follow_redirects=False,
        )
        self._failed_attempts = failed_attempts
        self._sleep = sleep

    def close(self) -> None:
        self._client.close()

    def _request_headers(self, request_id: str | None) -> dict[str, str]:
        headers = dict(self._headers)
        if request_id:
            headers["X-Request-Id"] = request_id
        return headers

    def _post(
        self, path: str, body: dict[str, Any], request_id: str | None, *, attempts: int = 1
    ) -> None:
        headers = self._request_headers(request_id)
        for attempt in range(1, attempts + 1):
            try:
                response = self._client.post(path, json=body, headers=headers)
            except httpx.TransportError as exc:
                if attempt == attempts:
                    raise SpringApiError(0, f"TRANSPORT_ERROR {type(exc).__name__}") from exc
                self._sleep(min(2**attempt, 10))
                continue
            if response.is_success:
                return
            if response.status_code in RETRYABLE_STATUS and attempt < attempts:
                retry_after = response.headers.get("Retry-After", "")
                self._sleep(float(retry_after) if retry_after.isdigit() else min(2**attempt, 10))
                continue
            raise SpringApiError(response.status_code, _error_code(response))
        raise SpringApiError(0, "RETRY_EXHAUSTED")  # pragma: no cover

    # INT-08. 한 번만 보낸다(모듈 설명 참고).
    def job_started(
        self, job_id: int, *, worker_id: str, started_at: datetime, request_id: str | None = None
    ) -> StartResult:
        """204면 STARTED, 409면 body.code에 따라 ALREADY_PROCESSING·ALREADY_FINISHED.

        409라도 code가 둘 다 아니면 SpringApiError를 그대로 올린다.
        """
        body = {"workerId": worker_id, "startedAt": started_at.isoformat()}
        try:
            self._post(f"/internal/v1/ai/jobs/{job_id}/started", body, request_id)
        except SpringApiError as exc:
            # 이미 시작했거나 끝난 작업의 메시지가 다시 전달된 것은 여기서 오류가 아니다.
            result = _START_CONFLICTS.get(exc.code) if exc.status == 409 else None
            if result is None:
                raise
            return result
        return StartResult.STARTED

    # INT-10. 마지막 보고라 전송 오류·5xx는 failed_attempts까지 다시 보낸다.
    def job_failed(
        self,
        job_id: int,
        *,
        error_code: OcrFailureCode,
        message: str,
        request_id: str | None = None,
    ) -> None:
        body = {
            "errorCode": error_code,
            # 요약만 보낸다. 개인정보·OCR 값·원본 키는 넣지 않는다.
            "message": message[:500],
            "retryable": error_code in RETRYABLE_FAILURES,
        }
        self._post(
            f"/internal/v1/ai/jobs/{job_id}/failed",
            body,
            request_id,
            attempts=self._failed_attempts,
        )

    # 재시도하지 않는다. httpx.TransportError(연결 실패·시간 초과),
    # SpringApiError(2xx가 아님)를 그대로 올린다.
    def document_source(
        self, document_id: int, *, timeout: float = 10.0, request_id: str | None = None
    ) -> DocumentSource:
        response = self._client.get(
            f"/internal/v1/ai-context/documents/{document_id}/source",
            headers=self._request_headers(request_id),
            timeout=timeout,
        )
        if not response.is_success:
            raise SpringApiError(response.status_code, _error_code(response))
        try:
            return DocumentSource.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise SpringApiError(response.status_code, "INVALID_BODY") from exc

    # 한 번만 보낸다. 같은 결과를 다시 보내면 Spring이 204로 받는다(멱등).
    def ocr_result(
        self, document_id: int, body: dict[str, Any], *, request_id: str | None = None
    ) -> None:
        self._post(f"/internal/v1/ai/documents/{document_id}/ocr-result", body, request_id)


def _error_code(response: httpx.Response) -> str:
    # 오류 본문의 code만 쓴다. message·details에는 값이 섞일 수 있어 읽지 않는다.
    try:
        payload = response.json()
    except ValueError:
        return "HTTP_ERROR"
    code = payload.get("code") if isinstance(payload, dict) else None
    return str(code) if code else "HTTP_ERROR"

"""Worker 테스트 대역(브로커·Spring·R2·엔진). 값은 모두 합성 데이터(가상 인물 Reyes)다."""

from __future__ import annotations

import asyncio
import atexit
import hashlib
import json
from collections.abc import Coroutine, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, TypeVar

from worker.handler import JobHandler
from worker.spring_client import DocumentSource, StartResult

T = TypeVar("T")

_loop: asyncio.AbstractEventLoop | None = None


def run(coro: Coroutine[Any, Any, T]) -> T:
    """테스트 전체에서 이벤트 루프 하나를 쓴다.

    Windows는 루프를 만들 때마다 소켓 쌍을 여는데, 일부 PC에서는 이 단계가 가끔 실패한다
    (WinError 10014). 테스트 대상과 무관한 실패라 루프를 다시 만들지 않는다.
    """
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        atexit.register(_loop.close)
    return _loop.run_until_complete(coro)


MESSAGE = {
    "schemaVersion": 1,
    "jobId": 7,
    "jobType": "OCR",
    "resourceType": "DOCUMENT",
    "resourceId": 42,
    "requestId": "req_abc",
}
BODY = json.dumps(MESSAGE).encode()
DELAYS = (5000, 15000, 45000)
WORKER_ID = "ocr-worker-test"

IMAGE = b"\x89PNG synthetic front image bytes"
IMAGE_SHA256 = hashlib.sha256(IMAGE).hexdigest()
OBJECT_KEY = "arc/front/3f1c2a9e-7b4d-4e8a-9c21-5d6f0b8a1e47.jpg"

# 합성 등록번호. 전체 번호는 Worker 밖으로 나가면 안 된다.
FULL_REGISTRATION_NO = "950815-5123456"
MASKED_REGISTRATION_NO = "950815-5******"


def engine_result(**overrides: Any) -> dict[str, Any]:
    """recognize() 출력 모양. overrides는 엔진 필드 이름 → value."""
    values: dict[str, Any] = {
        "NAME_EN": "MARK ANTHONY REYES",
        "NATIONALITY": "PHL",
        "BIRTH_DATE": "1995-08-15",
        "VISA_TYPE": "E-9",
        "STAY_EXPIRES_ON": None,
        "REGISTRATION_NO": MASKED_REGISTRATION_NO,
    }
    values.update(overrides)
    return {
        "modelVersion": "ppocrv5-ko-mdet-mrec",
        "latencyMs": 640,
        "fields": [
            {"name": name, "value": value, "confidence": None if value is None else 0.97}
            for name, value in values.items()
        ],
        # 로그용 크기 정보(#110). Spring 본문에는 들어가지 않는다.
        "image": {"original": "4032x3024", "processed": "1600x1200", "resize_ms": 41},
    }


@dataclass
class FakeDelivery:
    """aio-pika IncomingMessage 대역. ack/reject/nack 결과를 순서대로 남긴다."""

    body: bytes = BODY
    redelivered: bool | None = False
    headers: dict[str, Any] = field(default_factory=dict)
    message_id: str | None = "7"
    outcome: list[tuple[str, ...]] = field(default_factory=list)

    async def ack(self) -> None:
        self.outcome.append(("ack",))

    async def reject(self, requeue: bool = False) -> None:
        self.outcome.append(("reject", str(requeue)))

    async def nack(self, multiple: bool = False, requeue: bool = True) -> None:
        self.outcome.append(("nack", str(requeue)))


class FakeSpring:
    """SpringInternalClient 대역. 작업 상태 보고와 문서 API를 모두 흉내 낸다."""

    def __init__(
        self,
        start: StartResult = StartResult.STARTED,
        *,
        start_error: Exception | None = None,
        fail_error: Exception | None = None,
        source: DocumentSource | None = None,
        source_error: Exception | None = None,
        result_error: Exception | None = None,
    ) -> None:
        self.start = start
        self.start_error = start_error
        self.fail_error = fail_error
        self.source = source or DocumentSource(
            objectKey=OBJECT_KEY, side="FRONT", sha256=IMAGE_SHA256
        )
        self.source_error = source_error
        self.result_error = result_error
        self.started: list[tuple[int, str, str | None]] = []
        self.failed: list[tuple[int, str, str]] = []
        self.sources: list[int] = []
        self.results: list[tuple[int, dict[str, Any], str | None]] = []

    def job_started(
        self, job_id: int, *, worker_id: str, started_at: datetime, request_id: str | None = None
    ) -> StartResult:
        self.started.append((job_id, worker_id, request_id))
        if self.start_error:
            raise self.start_error
        return self.start

    def job_failed(
        self, job_id: int, *, error_code: Any, message: str, request_id: str | None = None
    ) -> None:
        self.failed.append((job_id, error_code, message))
        if self.fail_error:
            raise self.fail_error

    def document_source(
        self, document_id: int, *, timeout: float = 10.0, request_id: str | None = None
    ) -> DocumentSource:
        self.sources.append(document_id)
        if self.source_error:
            raise self.source_error
        return self.source

    def ocr_result(
        self, document_id: int, body: dict[str, Any], *, request_id: str | None = None
    ) -> None:
        self.results.append((document_id, body, request_id))
        if self.result_error:
            raise self.result_error


class FakePublisher:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.published: list[dict[str, Any]] = []

    async def publish_retry(
        self, body: bytes, *, headers: dict[str, Any], expiration: str, message_id: str | None
    ) -> None:
        if self.error:
            raise self.error
        self.published.append(
            {"body": body, "headers": headers, "expiration": expiration, "message_id": message_id}
        )


class FakeStore:
    """ArcImageStore 대역. 받은 키를 남기고 바이트를 쓴 뒤 SHA-256을 돌려준다."""

    def __init__(self, data: bytes = IMAGE, error: Exception | None = None) -> None:
        self.data = data
        self.error = error
        self.keys: list[str] = []

    def download(self, key: str, out: BinaryIO) -> str:
        self.keys.append(key)
        out.write(self.data[:4])  # 일부를 쓰다가 실패하는 경우도 임시 파일이 남지 않아야 한다
        if self.error:
            raise self.error
        out.write(self.data[4:])
        return hashlib.sha256(self.data).hexdigest()


class FakeEngine:
    """recognize() 대역. 부를 때 임시 파일이 있었는지와 내용을 남긴다."""

    def __init__(
        self, result: Mapping[str, Any] | None = None, error: Exception | None = None
    ) -> None:
        self.result = result if result is not None else engine_result()
        self.error = error
        self.calls: list[tuple[str, bool, bytes]] = []

    def __call__(self, front_path: str) -> Mapping[str, Any]:
        path = Path(front_path)
        self.calls.append((front_path, path.exists(), path.read_bytes() if path.exists() else b""))
        if self.error:
            raise self.error
        return self.result


def make_handler(
    spring: FakeSpring,
    processor: Any,
    publisher: FakePublisher | None = None,
) -> JobHandler:
    return JobHandler(
        spring=spring,
        processor=processor,
        publisher=publisher or FakePublisher(),
        retry_delays_ms=DELAYS,
        worker_id=WORKER_ID,
    )

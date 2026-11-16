"""Worker 메시지 처리 흐름: StartResult 분기, 재시도 헤더·expiration, DLQ."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.core.config import Settings
from app.integrations.spring_client import SpringApiError, StartResult
from app.worker import topology
from app.worker.handler import Decision, JobFailure, JobHandler, JobMessage, decide
from app.worker.retry import RETRY_COUNT_HEADER, retry_count, retry_expiration, retry_headers
from app.worker.runner import retry_message

pytestmark = pytest.mark.unit

MESSAGE = {
    "schemaVersion": 1,
    "jobId": 7,
    "jobType": "POLICY_INGEST",
    "resourceType": "POLICY_DOCUMENT",
    "resourceId": 42,
    "requestId": "req_abc",
}
BODY = json.dumps(MESSAGE).encode()
DELAYS = (5000, 15000, 45000)


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
    def __init__(
        self,
        start: StartResult = StartResult.STARTED,
        start_error: Exception | None = None,
        fail_error: Exception | None = None,
    ) -> None:
        self.start = start
        self.start_error = start_error
        self.fail_error = fail_error
        self.started: list[tuple[int, str, str | None]] = []
        self.failed: list[tuple[int, str, str]] = []

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


class FakeProcessor:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[JobMessage] = []

    def __call__(self, message: JobMessage) -> None:
        self.calls.append(message)
        if self.error:
            raise self.error


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


def _handler(
    spring: FakeSpring, processor: FakeProcessor, publisher: FakePublisher | None = None
) -> JobHandler:
    return JobHandler(
        job_type="POLICY_INGEST",
        resource_type="POLICY_DOCUMENT",
        spring=spring,
        processor=processor,
        publisher=publisher or FakePublisher(),
        retry_delays_ms=DELAYS,
        worker_id="policy-worker-test",
    )


TRANSIENT = JobFailure(
    "VECTOR_STORE_FAILED", transient=True, reason="store: ServerSelectionTimeoutError"
)


# ---------- 작업 1 리포트의 Worker 시나리오 5개 ----------


@pytest.mark.parametrize(
    ("start", "redelivered", "processed"),
    [
        pytest.param(StartResult.STARTED, False, True, id="1-first-delivery"),
        pytest.param(StartResult.ALREADY_PROCESSING, True, True, id="2-redelivered-after-crash"),
        pytest.param(
            StartResult.ALREADY_PROCESSING, False, False, id="3-duplicate-while-processing"
        ),
        pytest.param(StartResult.ALREADY_FINISHED, True, False, id="4-redelivered-after-finish"),
        pytest.param(StartResult.ALREADY_FINISHED, False, False, id="5-duplicate-after-finish"),
    ],
)
async def test_worker_scenarios(start: StartResult, redelivered: bool, processed: bool) -> None:
    spring, processor, publisher = FakeSpring(start), FakeProcessor(), FakePublisher()
    delivery = FakeDelivery(redelivered=redelivered)

    await _handler(spring, processor, publisher).handle(delivery)

    assert spring.started == [(7, "policy-worker-test", "req_abc")]
    assert [m.job_id for m in processor.calls] == ([7] if processed else [])
    assert delivery.outcome == [("ack",)]
    assert publisher.published == [] and spring.failed == []


def test_decide_table() -> None:
    assert decide(StartResult.STARTED, redelivered=False, retry_count=0) is Decision.PROCESS
    assert decide(StartResult.ALREADY_FINISHED, redelivered=True, retry_count=2) is Decision.SKIP
    assert (
        decide(StartResult.ALREADY_PROCESSING, redelivered=True, retry_count=0) is Decision.PROCESS
    )
    assert decide(StartResult.ALREADY_PROCESSING, redelivered=False, retry_count=0) is Decision.SKIP
    # retry 큐에서 돌아온 메시지는 redelivered가 아니어도 이어서 처리한다
    assert (
        decide(StartResult.ALREADY_PROCESSING, redelivered=False, retry_count=1) is Decision.PROCESS
    )


async def test_message_back_from_retry_queue_continues_processing() -> None:
    spring, processor = FakeSpring(StartResult.ALREADY_PROCESSING), FakeProcessor()
    delivery = FakeDelivery(redelivered=False, headers={RETRY_COUNT_HEADER: 1})

    await _handler(spring, processor).handle(delivery)

    assert len(processor.calls) == 1
    assert delivery.outcome == [("ack",)]


# ---------- 재시도 ----------


@pytest.mark.parametrize(
    ("count", "next_count", "expiration"),
    [(0, 1, "5000"), (1, 2, "15000"), (2, 3, "45000")],
)
async def test_transient_failure_republishes_with_next_count_and_expiration(
    count: int, next_count: int, expiration: str
) -> None:
    spring, publisher = FakeSpring(StartResult.ALREADY_PROCESSING), FakePublisher()
    headers = {
        RETRY_COUNT_HEADER: count,
        "x-death": [{"queue": "policy.ingest.retry"}],
        "keep": "v",
    }
    if count == 0:
        spring.start = StartResult.STARTED
        headers = {"keep": "v"}
    delivery = FakeDelivery(headers=headers)

    await _handler(spring, FakeProcessor(TRANSIENT), publisher).handle(delivery)

    assert publisher.published == [
        {
            "body": BODY,
            "headers": {"keep": "v", RETRY_COUNT_HEADER: next_count},
            "expiration": expiration,
            "message_id": "7",
        }
    ]
    assert delivery.outcome == [("ack",)]  # 재발행 뒤 원본 ack
    assert spring.failed == []


async def test_retry_exhausted_reports_retryable_code_and_rejects_to_dlq() -> None:
    spring, publisher = FakeSpring(StartResult.ALREADY_PROCESSING), FakePublisher()
    delivery = FakeDelivery(headers={RETRY_COUNT_HEADER: 3})

    await _handler(spring, FakeProcessor(TRANSIENT), publisher).handle(delivery)

    assert publisher.published == []
    assert spring.failed == [(7, "VECTOR_STORE_FAILED", "store: ServerSelectionTimeoutError")]
    assert delivery.outcome == [("reject", "False")]


async def test_permanent_failure_reports_and_rejects_without_retry() -> None:
    spring, publisher = FakeSpring(), FakePublisher()
    failure = JobFailure(
        "HASH_MISMATCH", transient=False, reason="R2 bytes differ from content_hash"
    )
    delivery = FakeDelivery()

    await _handler(spring, FakeProcessor(failure), publisher).handle(delivery)

    assert publisher.published == []
    assert spring.failed == [(7, "HASH_MISMATCH", "R2 bytes differ from content_hash")]
    assert delivery.outcome == [("reject", "False")]


async def test_unexpected_error_is_reported_as_worker_error() -> None:
    spring, delivery = FakeSpring(), FakeDelivery()

    await _handler(spring, FakeProcessor(KeyError("x"))).handle(delivery)

    assert spring.failed == [(7, "WORKER_ERROR", "KeyError")]
    assert delivery.outcome == [("reject", "False")]


@pytest.mark.parametrize(
    "error",
    [
        SpringApiError(503, "INTERNAL_ERROR", ""),
        SpringApiError(0, "TRANSPORT_ERROR", "ConnectError"),
    ],
)
async def test_job_started_transient_error_is_retried_without_processing(error: Exception) -> None:
    spring, processor, publisher = FakeSpring(start_error=error), FakeProcessor(), FakePublisher()
    delivery = FakeDelivery()

    await _handler(spring, processor, publisher).handle(delivery)

    assert processor.calls == []
    assert [p["headers"][RETRY_COUNT_HEADER] for p in publisher.published] == [1]
    assert publisher.published[0]["expiration"] == "5000"
    assert delivery.outcome == [("ack",)]


async def test_job_started_rejected_goes_to_dlq_without_job_failed() -> None:
    spring = FakeSpring(start_error=SpringApiError(404, "RESOURCE_NOT_FOUND", ""))
    delivery = FakeDelivery()

    await _handler(spring, FakeProcessor()).handle(delivery)

    assert spring.failed == []
    assert delivery.outcome == [("reject", "False")]


async def test_retry_publish_failure_requeues_original() -> None:
    spring, publisher = FakeSpring(), FakePublisher(error=ConnectionError("channel closed"))
    delivery = FakeDelivery()

    await _handler(spring, FakeProcessor(TRANSIENT), publisher).handle(delivery)

    assert delivery.outcome == [("nack", "True")]
    assert spring.failed == []


async def test_job_failed_report_error_still_rejects_to_dlq() -> None:
    spring = FakeSpring(fail_error=httpx.ConnectError("down"))
    failure = JobFailure("EXTRACTION_FAILED", transient=False, reason="extract: failed none")
    delivery = FakeDelivery()

    await _handler(spring, FakeProcessor(failure)).handle(delivery)

    assert delivery.outcome == [("reject", "False")]


# ---------- 본문 검증 ----------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"not json", id="not-json"),
        pytest.param(json.dumps({**MESSAGE, "schemaVersion": 2}).encode(), id="schema-version"),
        pytest.param(json.dumps({**MESSAGE, "jobType": "OCR"}).encode(), id="job-type"),
        pytest.param(
            json.dumps({**MESSAGE, "resourceType": "DOCUMENT"}).encode(), id="resource-type"
        ),
        pytest.param(
            json.dumps({k: v for k, v in MESSAGE.items() if k != "jobId"}).encode(), id="no-job-id"
        ),
        pytest.param(json.dumps({**MESSAGE, "resourceId": None}).encode(), id="no-resource-id"),
        pytest.param(json.dumps({**MESSAGE, "extra": 1}).encode(), id="unknown-field"),
    ],
)
async def test_invalid_message_goes_to_dlq_without_spring_calls(body: bytes) -> None:
    spring, processor = FakeSpring(), FakeProcessor()
    delivery = FakeDelivery(body=body)

    await _handler(spring, processor).handle(delivery)

    assert delivery.outcome == [("reject", "False")]
    assert spring.started == [] and spring.failed == [] and processor.calls == []


# ---------- 재시도 표·헤더 ----------


def test_retry_expiration_table() -> None:
    assert [retry_expiration(n, DELAYS) for n in range(4)] == ["5000", "15000", "45000", None]
    assert retry_expiration(0, (1000,)) == "1000"
    assert retry_expiration(1, (1000,)) is None


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (None, 0),
        ({}, 0),
        ({RETRY_COUNT_HEADER: 2}, 2),
        ({RETRY_COUNT_HEADER: "3"}, 3),
        ({RETRY_COUNT_HEADER: "x"}, 0),
    ],
)
def test_retry_count(headers: dict[str, Any] | None, expected: int) -> None:
    assert retry_count(headers) == expected


def test_retry_headers_drop_broker_death_headers() -> None:
    headers = {"x-death": [], "x-first-death-queue": "q", "x-last-death-reason": "expired", "a": 1}
    assert retry_headers(headers, 2) == {"a": 1, RETRY_COUNT_HEADER: 2}


def test_retry_delays_setting_parses_comma_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETRY_DELAYS_MS", "1000,2000")
    assert Settings(_env_file=None).retry_delays_ms == (1000, 2000)  # type: ignore[call-arg]
    monkeypatch.delenv("RETRY_DELAYS_MS")
    assert Settings(_env_file=None).retry_delays_ms == (5000, 15000, 45000)  # type: ignore[call-arg]
    monkeypatch.setenv("RETRY_DELAYS_MS", "5000,0")
    with pytest.raises(ValueError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_retry_message_is_persistent_with_ms_expiration() -> None:
    message = retry_message(
        BODY, headers={RETRY_COUNT_HEADER: 1}, expiration="15000", message_id="7"
    )
    properties = message.properties
    assert properties.expiration == "15000"
    assert properties.delivery_mode == 2
    assert properties.headers == {RETRY_COUNT_HEADER: 1}
    assert properties.message_id == "7"
    assert properties.content_type == "application/json"


# ---------- 큐 선언(BE와 같은 인자) ----------


def test_queue_arguments_match_backend() -> None:
    assert topology.POLICY_INGEST.queue_arguments() == {
        "policy.ingest": {
            "x-dead-letter-exchange": "hanstep.ai",
            "x-dead-letter-routing-key": "policy.ingest.dlq",
        },
        "policy.ingest.retry": {
            "x-dead-letter-exchange": "hanstep.ai",
            "x-dead-letter-routing-key": "policy.ingest",
        },
        "policy.ingest.dlq": {},
    }


async def test_declare_uses_durable_queues_and_binds_each_name() -> None:
    channel = MagicMock()
    channel.declare_exchange = AsyncMock(return_value="exchange")
    queues: dict[str, MagicMock] = {}

    async def declare_queue(name: str, **kwargs: Any) -> MagicMock:
        queue = MagicMock(name=name)
        queue.bind = AsyncMock()
        queue.kwargs = kwargs
        queues[name] = queue
        return queue

    channel.declare_queue = declare_queue

    exchange, work = await topology.declare(channel, topology.POLICY_INGEST)

    assert exchange == "exchange"
    assert work is queues["policy.ingest"]
    channel.declare_exchange.assert_awaited_once()
    assert channel.declare_exchange.await_args.kwargs == {"durable": True}
    for name, queue in queues.items():
        assert queue.kwargs["durable"] is True
        queue.bind.assert_awaited_once_with("exchange", routing_key=name)
    # TTL 없음(backend #74)
    assert "x-message-ttl" not in (queues["policy.ingest.retry"].kwargs["arguments"] or {})
    assert queues["policy.ingest.dlq"].kwargs["arguments"] is None

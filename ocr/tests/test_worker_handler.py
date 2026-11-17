"""Worker 메시지 처리 흐름: job_started 분기, 재시도 헤더·expiration, DLQ, 큐 선언."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from worker_fakes import (
    BODY,
    DELAYS,
    MESSAGE,
    WORKER_ID,
    FakeDelivery,
    FakePublisher,
    FakeSpring,
    make_handler,
    run,
)

from worker import topology
from worker.config import Settings
from worker.handler import Decision, JobFailure, JobMessage, decide
from worker.retry import RETRY_COUNT_HEADER, retry_count, retry_expiration, retry_headers
from worker.runner import retry_message
from worker.spring_client import SpringApiError, StartResult

pytestmark = pytest.mark.unit


class FakeProcessor:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[JobMessage] = []

    def __call__(self, message: JobMessage) -> None:
        self.calls.append(message)
        if self.error:
            raise self.error


TRANSIENT = JobFailure("SOURCE_FETCH_FAILED", transient=True, reason="fetch: R2 responded 503")


def handle(
    spring: FakeSpring,
    processor: FakeProcessor,
    delivery: FakeDelivery,
    publisher: FakePublisher | None = None,
) -> None:
    run(make_handler(spring, processor, publisher).handle(delivery))


# ---------- job_started 분기 5가지 ----------


@pytest.mark.parametrize(
    ("start", "redelivered", "headers", "processed"),
    [
        pytest.param(StartResult.STARTED, False, {}, True, id="1-started"),
        pytest.param(StartResult.ALREADY_FINISHED, True, {}, False, id="2-already-finished"),
        pytest.param(StartResult.ALREADY_PROCESSING, True, {}, True, id="3-processing-redelivered"),
        pytest.param(
            StartResult.ALREADY_PROCESSING,
            False,
            {RETRY_COUNT_HEADER: 1},
            True,
            id="4-processing-back-from-retry-queue",
        ),
        pytest.param(StartResult.ALREADY_PROCESSING, False, {}, False, id="5-processing-duplicate"),
    ],
)
def test_job_started_branches(
    start: StartResult, redelivered: bool, headers: dict[str, Any], processed: bool
) -> None:
    spring, processor, publisher = FakeSpring(start), FakeProcessor(), FakePublisher()
    delivery = FakeDelivery(redelivered=redelivered, headers=headers)

    handle(spring, processor, delivery, publisher)

    assert spring.started == [(7, WORKER_ID, "req_abc")]
    assert [m.resource_id for m in processor.calls] == ([42] if processed else [])
    assert delivery.outcome == [("ack",)]
    assert publisher.published == [] and spring.failed == []


def test_decide_table() -> None:
    assert decide(StartResult.STARTED, redelivered=False, retry_count=0) is Decision.PROCESS
    assert decide(StartResult.ALREADY_FINISHED, redelivered=True, retry_count=2) is Decision.SKIP
    assert decide(StartResult.ALREADY_FINISHED, redelivered=False, retry_count=0) is Decision.SKIP
    assert (
        decide(StartResult.ALREADY_PROCESSING, redelivered=True, retry_count=0) is Decision.PROCESS
    )
    assert (
        decide(StartResult.ALREADY_PROCESSING, redelivered=False, retry_count=1) is Decision.PROCESS
    )
    assert decide(StartResult.ALREADY_PROCESSING, redelivered=False, retry_count=0) is Decision.SKIP


# ---------- 재시도 ----------


@pytest.mark.parametrize(
    ("count", "next_count", "expiration"),
    [(0, 1, "5000"), (1, 2, "15000"), (2, 3, "45000")],
)
def test_transient_failure_republishes_with_next_count_and_expiration(
    count: int, next_count: int, expiration: str
) -> None:
    start = StartResult.STARTED if count == 0 else StartResult.ALREADY_PROCESSING
    spring, publisher = FakeSpring(start), FakePublisher()
    headers: dict[str, Any] = {"keep": "v"}
    if count:
        headers.update({RETRY_COUNT_HEADER: count, "x-death": [{"queue": "ocr.request.retry"}]})
    delivery = FakeDelivery(headers=headers)

    handle(spring, FakeProcessor(TRANSIENT), delivery, publisher)

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


def test_retry_cycle_until_dlq() -> None:
    """같은 메시지가 일시 오류를 계속 내면 5000·15000·45000 뒤 job_failed → DLQ."""
    publisher = FakePublisher()
    delivery = FakeDelivery()
    expirations: list[str] = []
    for _ in range(4):
        start = StartResult.ALREADY_PROCESSING if delivery.headers else StartResult.STARTED
        spring = FakeSpring(start)
        handle(spring, FakeProcessor(TRANSIENT), delivery, publisher)
        if delivery.outcome[-1] == ("ack",):
            republished = publisher.published[-1]
            expirations.append(republished["expiration"])
            delivery = FakeDelivery(body=republished["body"], headers=republished["headers"])

    assert expirations == ["5000", "15000", "45000"]
    assert delivery.headers[RETRY_COUNT_HEADER] == 3
    assert spring.failed == [(7, "SOURCE_FETCH_FAILED", "fetch: R2 responded 503")]
    assert delivery.outcome == [("reject", "False")]


def test_retry_exhausted_reports_and_rejects_to_dlq() -> None:
    spring, publisher = FakeSpring(StartResult.ALREADY_PROCESSING), FakePublisher()
    delivery = FakeDelivery(headers={RETRY_COUNT_HEADER: 3})

    handle(spring, FakeProcessor(TRANSIENT), delivery, publisher)

    assert publisher.published == []
    assert spring.failed == [(7, "SOURCE_FETCH_FAILED", "fetch: R2 responded 503")]
    assert delivery.outcome == [("reject", "False")]


def test_permanent_failure_reports_and_rejects_without_retry() -> None:
    spring, publisher = FakeSpring(), FakePublisher()
    failure = JobFailure("OCR_FAILED", transient=False, reason="no required field recognized")
    delivery = FakeDelivery()

    handle(spring, FakeProcessor(failure), delivery, publisher)

    assert publisher.published == []
    assert spring.failed == [(7, "OCR_FAILED", "no required field recognized")]
    assert delivery.outcome == [("reject", "False")]


def test_unexpected_error_is_worker_error_and_log_has_no_exception_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    spring, delivery = FakeSpring(), FakeDelivery()

    with caplog.at_level(logging.INFO):
        handle(spring, FakeProcessor(KeyError("MARK ANTHONY REYES")), delivery)

    assert spring.failed == [(7, "WORKER_ERROR", "KeyError")]
    assert delivery.outcome == [("reject", "False")]
    assert "KeyError" in caplog.text
    assert "REYES" not in caplog.text


@pytest.mark.parametrize(
    "error",
    [
        SpringApiError(503, "INTERNAL_ERROR"),
        SpringApiError(0, "TRANSPORT_ERROR ConnectError"),
        httpx.ReadTimeout("timed out"),
    ],
)
def test_job_started_transient_error_is_retried_without_processing(error: Exception) -> None:
    spring, processor, publisher = FakeSpring(start_error=error), FakeProcessor(), FakePublisher()
    delivery = FakeDelivery()

    handle(spring, processor, delivery, publisher)

    assert processor.calls == []
    assert [p["headers"][RETRY_COUNT_HEADER] for p in publisher.published] == [1]
    assert publisher.published[0]["expiration"] == "5000"
    assert delivery.outcome == [("ack",)]
    assert spring.failed == []


def test_job_started_rejected_goes_to_dlq_without_job_failed() -> None:
    spring = FakeSpring(start_error=SpringApiError(404, "RESOURCE_NOT_FOUND"))
    delivery = FakeDelivery()

    handle(spring, FakeProcessor(), delivery)

    assert spring.failed == []
    assert delivery.outcome == [("reject", "False")]


def test_retry_publish_failure_requeues_original() -> None:
    spring, publisher = FakeSpring(), FakePublisher(error=ConnectionError("channel closed"))
    delivery = FakeDelivery()

    handle(spring, FakeProcessor(TRANSIENT), delivery, publisher)

    assert delivery.outcome == [("nack", "True")]
    assert spring.failed == []


def test_job_failed_report_error_still_rejects_to_dlq() -> None:
    spring = FakeSpring(fail_error=httpx.ConnectError("down"))
    failure = JobFailure("HASH_MISMATCH", transient=False, reason="R2 bytes differ from sha256")
    delivery = FakeDelivery()

    handle(spring, FakeProcessor(failure), delivery)

    assert delivery.outcome == [("reject", "False")]


# ---------- 본문 검증(job_failed 없이 DLQ) ----------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"not json", id="not-json"),
        pytest.param(json.dumps({**MESSAGE, "schemaVersion": 2}).encode(), id="schema-version"),
        pytest.param(json.dumps({**MESSAGE, "jobType": "POLICY_INGEST"}).encode(), id="job-type"),
        pytest.param(
            json.dumps({**MESSAGE, "resourceType": "POLICY_DOCUMENT"}).encode(), id="resource-type"
        ),
        pytest.param(
            json.dumps({k: v for k, v in MESSAGE.items() if k != "jobId"}).encode(), id="no-job-id"
        ),
        pytest.param(json.dumps({**MESSAGE, "resourceId": None}).encode(), id="no-resource-id"),
        pytest.param(json.dumps({**MESSAGE, "resourceId": 0}).encode(), id="zero-resource-id"),
        pytest.param(json.dumps({**MESSAGE, "extra": 1}).encode(), id="unknown-field"),
    ],
)
def test_invalid_message_goes_to_dlq_without_spring_calls(body: bytes) -> None:
    spring, processor = FakeSpring(), FakeProcessor()
    delivery = FakeDelivery(body=body)

    handle(spring, processor, delivery)

    assert delivery.outcome == [("reject", "False")]
    assert spring.started == [] and spring.failed == [] and processor.calls == []


def test_message_without_request_id_is_accepted() -> None:
    body = json.dumps({k: v for k, v in MESSAGE.items() if k != "requestId"}).encode()
    spring, processor = FakeSpring(), FakeProcessor()

    handle(spring, processor, FakeDelivery(body=body))

    assert spring.started == [(7, WORKER_ID, None)]
    assert len(processor.calls) == 1


# ---------- 재시도 표·헤더·메시지 ----------


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
        ({RETRY_COUNT_HEADER: -1}, 0),
    ],
)
def test_retry_count(headers: dict[str, Any] | None, expected: int) -> None:
    assert retry_count(headers) == expected


def test_retry_headers_drop_broker_death_headers() -> None:
    headers = {"x-death": [], "x-first-death-queue": "q", "x-last-death-reason": "expired", "a": 1}
    assert retry_headers(headers, 2) == {"a": 1, RETRY_COUNT_HEADER: 2}


@pytest.mark.parametrize("expiration", ["5000", "15000", "45000"])
def test_retry_message_is_persistent_with_ms_expiration(expiration: str) -> None:
    message = retry_message(
        BODY, headers={RETRY_COUNT_HEADER: 1}, expiration=expiration, message_id="7"
    )
    properties = message.properties
    assert properties.expiration == expiration
    assert properties.delivery_mode == 2
    assert properties.headers == {RETRY_COUNT_HEADER: 1}
    assert properties.message_id == "7"
    assert properties.content_type == "application/json"
    assert message.body == BODY


# ---------- 설정 ----------


def test_retry_delays_setting_parses_comma_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETRY_DELAYS_MS", "1000,2000")
    assert Settings().retry_delays_ms == (1000, 2000)
    monkeypatch.delenv("RETRY_DELAYS_MS")
    assert Settings().retry_delays_ms == (5000, 15000, 45000)
    monkeypatch.setenv("RETRY_DELAYS_MS", "5000,0")
    with pytest.raises(ValueError):
        Settings()


def test_missing_settings_lists_unset_and_placeholder_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "RABBITMQ_URL",
        "SPRING_INTERNAL_API_URL",
        "SPRING_INTERNAL_API_KEY",
        "R2_ENDPOINT_URL",
        "R2_OCR_ACCESS_KEY_ID",
        "R2_OCR_SECRET_ACCESS_KEY",
        "R2_ARC_BUCKET",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RABBITMQ_URL", "amqp://hanstep:CHANGE_ME_RABBIT_PASSWORD@rabbitmq:5672/")
    assert Settings().missing() == [
        "RABBITMQ_URL",
        "SPRING_INTERNAL_API_URL/SPRING_INTERNAL_API_KEY",
        "R2_ENDPOINT_URL/R2_OCR_ACCESS_KEY_ID/R2_OCR_SECRET_ACCESS_KEY/R2_ARC_BUCKET",
    ]

    monkeypatch.setenv("RABBITMQ_URL", "amqp://u:p@rabbitmq:5672/")
    monkeypatch.setenv("SPRING_INTERNAL_API_URL", "http://host.docker.internal:8080")
    monkeypatch.setenv("SPRING_INTERNAL_API_KEY", "k" * 32)
    monkeypatch.setenv("R2_ENDPOINT_URL", "https://acct.r2.cloudflarestorage.com")
    monkeypatch.setenv("R2_OCR_ACCESS_KEY_ID", "id")
    monkeypatch.setenv("R2_OCR_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("R2_ARC_BUCKET", "hanstep-arc-private")
    assert Settings().missing() == []


def test_r2_endpoint_with_bucket_path_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "R2_ENDPOINT_URL", "https://acct.r2.cloudflarestorage.com/hanstep-arc-private"
    )
    with pytest.raises(ValueError):
        Settings()


def test_settings_do_not_read_env_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """ocr/.env를 두지 않는다. 작업 폴더에 .env가 있어도 읽지 않고 환경 변수만 본다."""
    monkeypatch.delenv("RABBITMQ_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("RABBITMQ_URL=amqp://u:p@rabbitmq:5672/\n", encoding="utf-8")

    assert "RABBITMQ_URL" in Settings().missing()


# ---------- 큐 선언(BE와 같은 인자) ----------


def test_queue_arguments_match_backend() -> None:
    assert topology.OCR.queue_arguments() == {
        "ocr.request": {
            "x-dead-letter-exchange": "hanstep.ai",
            "x-dead-letter-routing-key": "ocr.request.dlq",
        },
        "ocr.request.retry": {
            "x-dead-letter-exchange": "hanstep.ai",
            "x-dead-letter-routing-key": "ocr.request",
        },
        "ocr.request.dlq": {},
    }


def test_declare_uses_durable_queues_and_binds_each_name() -> None:
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

    exchange, work = run(topology.declare(channel, topology.OCR))

    assert exchange == "exchange"
    assert work is queues["ocr.request"]
    channel.declare_exchange.assert_awaited_once()
    assert channel.declare_exchange.await_args.args[0] == "hanstep.ai"
    assert channel.declare_exchange.await_args.kwargs == {"durable": True}
    assert set(queues) == {"ocr.request", "ocr.request.retry", "ocr.request.dlq"}
    for name, queue in queues.items():
        assert queue.kwargs["durable"] is True
        queue.bind.assert_awaited_once_with("exchange", routing_key=name)
    # TTL 없음(backend #74)
    assert "x-message-ttl" not in (queues["ocr.request.retry"].kwargs["arguments"] or {})
    assert queues["ocr.request.dlq"].kwargs["arguments"] is None

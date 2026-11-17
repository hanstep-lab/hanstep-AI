"""Spring 내부 API 클라이언트(가짜 HTTP)와 R2 저장소(가짜 boto3 클라이언트)."""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from worker_fakes import IMAGE, IMAGE_SHA256, OBJECT_KEY

from worker import r2
from worker.r2 import ImageTooLarge, R2ArcImageStore
from worker.spring_client import SpringApiError, SpringInternalClient, StartResult

pytestmark = pytest.mark.unit

STARTED_AT = datetime(2026, 11, 17, 3, 0, tzinfo=timezone.utc)


class Recorder:
    """httpx.MockTransport 처리기. 응답을 순서대로 돌려주고 요청을 남긴다."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def client(recorder: Recorder, sleeps: list[float] | None = None) -> SpringInternalClient:
    sleep: Callable[[float], None] = sleeps.append if sleeps is not None else lambda _: None
    return SpringInternalClient(
        base_url="http://spring.test",
        api_key="k" * 32,
        client=httpx.Client(base_url="http://spring.test", transport=httpx.MockTransport(recorder)),
        sleep=sleep,
    )


def error(status: int, code: str) -> httpx.Response:
    # message·details에는 값이 섞일 수 있다. 클라이언트는 code만 읽어야 한다
    return httpx.Response(
        status, json={"code": code, "message": "950815-5123456", "details": {"x": "y"}}
    )


# ---------- INT-08 ----------


def test_job_started_204_is_started_with_headers_and_body() -> None:
    recorder = Recorder(httpx.Response(204))

    result = client(recorder).job_started(
        7, worker_id="ocr-worker-1", started_at=STARTED_AT, request_id="req_abc"
    )

    assert result is StartResult.STARTED
    [request] = recorder.requests
    assert request.method == "POST" and request.url.path == "/internal/v1/ai/jobs/7/started"
    assert request.headers["X-Internal-Token"] == "k" * 32
    assert request.headers["X-Request-Id"] == "req_abc"
    assert json.loads(request.content) == {
        "workerId": "ocr-worker-1",
        "startedAt": "2026-11-17T03:00:00+00:00",
    }


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("JOB_ALREADY_PROCESSING", StartResult.ALREADY_PROCESSING),
        ("JOB_ALREADY_FINISHED", StartResult.ALREADY_FINISHED),
    ],
)
def test_job_started_409_codes(code: str, expected: StartResult) -> None:
    recorder = Recorder(error(409, code))
    assert client(recorder).job_started(7, worker_id="w", started_at=STARTED_AT) is expected


def test_job_started_unknown_409_raises() -> None:
    with pytest.raises(SpringApiError) as caught:
        client(Recorder(error(409, "INVALID_STATE"))).job_started(
            7, worker_id="w", started_at=STARTED_AT
        )
    assert (caught.value.status, caught.value.code) == (409, "INVALID_STATE")
    assert "950815" not in str(caught.value)


@pytest.mark.parametrize(
    "response",
    [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), error(503, "INTERNAL_ERROR")],
)
def test_job_started_is_sent_once_and_queue_retries(response: httpx.Response | Exception) -> None:
    """클라이언트가 다시 보내면 첫 요청이 반영된 경우 409 처리 중으로 보여 작업이 멈춘다."""
    recorder = Recorder(response, httpx.Response(204))
    with pytest.raises(SpringApiError) as caught:
        client(recorder).job_started(7, worker_id="w", started_at=STARTED_AT)
    assert len(recorder.requests) == 1
    assert caught.value.status in (0, 503)


# ---------- INT-10 ----------


def test_job_failed_retries_transient_errors_then_succeeds() -> None:
    sleeps: list[float] = []
    recorder = Recorder(
        httpx.ConnectError("refused"), error(503, "INTERNAL_ERROR"), httpx.Response(204)
    )

    client(recorder, sleeps).job_failed(
        7, error_code="OCR_FAILED", message="x" * 600, request_id="req_abc"
    )

    assert len(recorder.requests) == 3 and sleeps == [2, 4]
    assert recorder.requests[0].url.path == "/internal/v1/ai/jobs/7/failed"
    assert json.loads(recorder.requests[-1].content) == {
        "errorCode": "OCR_FAILED",
        "message": "x" * 500,
        "retryable": False,
    }


def test_job_failed_marks_retryable_codes() -> None:
    recorder = Recorder(httpx.Response(204))
    client(recorder).job_failed(7, error_code="SOURCE_FETCH_FAILED", message="fetch")
    assert json.loads(recorder.requests[0].content)["retryable"] is True


def test_job_failed_gives_up_after_three_attempts() -> None:
    recorder = Recorder(*(error(503, "INTERNAL_ERROR") for _ in range(3)))
    with pytest.raises(SpringApiError):
        client(recorder).job_failed(7, error_code="OCR_FAILED", message="m")
    assert len(recorder.requests) == 3


# ---------- 원본 위치·결과 보고 ----------


def test_document_source_parses_response() -> None:
    recorder = Recorder(
        httpx.Response(200, json={"objectKey": OBJECT_KEY, "side": "FRONT", "sha256": IMAGE_SHA256})
    )

    source = client(recorder).document_source(42, request_id="req_abc")

    assert (source.object_key, source.side, source.sha256) == (OBJECT_KEY, "FRONT", IMAGE_SHA256)
    [request] = recorder.requests
    assert request.method == "GET"
    assert request.url.path == "/internal/v1/ai-context/documents/42/source"
    assert request.headers["X-Request-Id"] == "req_abc"


def test_document_source_null_object_key() -> None:
    recorder = Recorder(
        httpx.Response(200, json={"objectKey": None, "side": "FRONT", "sha256": IMAGE_SHA256})
    )
    assert client(recorder).document_source(42).object_key is None


@pytest.mark.parametrize(
    "body",
    [
        {"objectKey": OBJECT_KEY, "side": "FRONT"},
        {"objectKey": OBJECT_KEY, "side": "FRONT", "sha256": IMAGE_SHA256.upper()},
        [],
    ],
)
def test_document_source_invalid_body(body: Any) -> None:
    with pytest.raises(SpringApiError) as caught:
        client(Recorder(httpx.Response(200, json=body))).document_source(42)
    assert caught.value.code == "INVALID_BODY"


def test_document_source_404_is_not_retried() -> None:
    recorder = Recorder(error(404, "RESOURCE_NOT_FOUND"))
    with pytest.raises(SpringApiError) as caught:
        client(recorder).document_source(42)
    assert (caught.value.status, caught.value.code) == (404, "RESOURCE_NOT_FOUND")
    assert len(recorder.requests) == 1


def test_ocr_result_posts_body_once() -> None:
    recorder = Recorder(error(400, "VALIDATION_ERROR"))
    body = {"documentId": 42, "side": "FRONT", "fields": []}

    with pytest.raises(SpringApiError) as caught:
        client(recorder).ocr_result(42, body, request_id="req_abc")

    assert caught.value.code == "VALIDATION_ERROR"
    [request] = recorder.requests
    assert request.url.path == "/internal/v1/ai/documents/42/ocr-result"
    assert json.loads(request.content) == body


def test_error_code_falls_back_when_body_is_not_json() -> None:
    with pytest.raises(SpringApiError) as caught:
        client(Recorder(httpx.Response(502, text="bad gateway"))).ocr_result(42, {})
    assert (caught.value.status, caught.value.code) == (502, "HTTP_ERROR")


# ---------- R2 ----------


class FakeBody:
    def __init__(self, data: bytes) -> None:
        self._stream = io.BytesIO(data)

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)


class FakeS3:
    def __init__(self, data: bytes = IMAGE, error: Exception | None = None) -> None:
        self.data = data
        self.error = error
        self.calls: list[dict[str, str]] = []

    def get_object(self, **kwargs: str) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"Body": FakeBody(self.data)}


def store(s3: FakeS3) -> R2ArcImageStore:
    return R2ArcImageStore(
        endpoint_url="https://acct.r2.cloudflarestorage.com",
        access_key_id="id",
        secret_access_key="secret",
        bucket="hanstep-arc-private",
        client=s3,
    )


def client_error(code: str, status: int) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": ""}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "GetObject",
    )


def test_r2_download_streams_bytes_and_returns_sha256() -> None:
    s3, out = FakeS3(), io.BytesIO()

    digest = store(s3).download(OBJECT_KEY, out)

    assert out.getvalue() == IMAGE
    assert digest == hashlib.sha256(IMAGE).hexdigest()
    assert s3.calls == [{"Bucket": "hanstep-arc-private", "Key": OBJECT_KEY}]


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (client_error("NoSuchKey", 404), LookupError),
        (client_error("InternalError", 500), ConnectionError),
        (
            EndpointConnectionError(endpoint_url="https://acct.r2.cloudflarestorage.com"),
            ConnectionError,
        ),
    ],
)
def test_r2_errors_are_classified(raised: Exception, expected: type[Exception]) -> None:
    with pytest.raises(expected):
        store(FakeS3(error=raised)).download(OBJECT_KEY, io.BytesIO())


def test_r2_access_denied_is_raised_as_is() -> None:
    with pytest.raises(ClientError):
        store(FakeS3(error=client_error("AccessDenied", 403))).download(OBJECT_KEY, io.BytesIO())


def test_r2_object_over_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(r2, "MAX_IMAGE_BYTES", len(IMAGE) - 1)
    with pytest.raises(ImageTooLarge):
        store(FakeS3()).download(OBJECT_KEY, io.BytesIO())

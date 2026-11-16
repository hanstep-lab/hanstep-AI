from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.integrations.policy_gateway import ManifestPolicyGateway
from app.integrations.spring_client import (
    PolicyIngestedReport,
    SpringApiError,
    SpringInternalClient,
    StartResult,
)
from app.rag.errors import ContractBlockedError
from app.rag.ingestion import SourceResult, ingested_report
from app.rag.manifest import SourceEntry

pytestmark = pytest.mark.unit

KEY = "s" * 40
NOW = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)


def _client(handler: Any, sleeps: list[float] | None = None) -> SpringInternalClient:
    return SpringInternalClient(
        base_url="http://spring.test",
        api_key=KEY,
        client=httpx.Client(base_url="http://spring.test", transport=httpx.MockTransport(handler)),
        sleep=(sleeps if sleeps is not None else []).append,
    )


def _report() -> PolicyIngestedReport:
    return PolicyIngestedReport(
        document_version="E74-2026-v1",
        content_hash="a" * 64,
        chunk_count=41,
        mongo_collection="visa_policy_chunks",
        embedding_model="BAAI/bge-m3",
        embedding_revision="5617a9f61b028005a4858fdac845db406aefb181",
        object_key="policy-sources/visa/sha256/" + "a" * 64 + ".pdf",
        ingested_at=NOW,
    )


def test_policy_ingested_posts_camel_case_body_with_token_and_request_id() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(
            path=request.url.path,
            token=request.headers["X-Internal-Token"],
            rid=request.headers["X-Request-Id"],
            body=json.loads(request.content),
        )
        return httpx.Response(204)

    _client(handler).policy_ingested(12, _report(), request_id="run-1")
    assert seen["path"] == "/internal/v1/ai/policies/12/ingested"
    assert (seen["token"], seen["rid"]) == (KEY, "run-1")
    assert seen["body"] == {
        "documentVersion": "E74-2026-v1",
        "contentHash": "a" * 64,
        "chunkCount": 41,
        "mongoCollection": "visa_policy_chunks",
        "embeddingModel": "BAAI/bge-m3",
        "embeddingRevision": "5617a9f61b028005a4858fdac845db406aefb181",
        "objectKey": "policy-sources/visa/sha256/" + "a" * 64 + ".pdf",
        "ingestedAt": "2026-11-09T10:00:00+00:00",
    }


def test_job_started_204_means_this_worker_started() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/internal/v1/ai/jobs/7/started"
        assert json.loads(request.content) == {
            "workerId": "policy-worker-1",
            "startedAt": NOW.isoformat(),
        }
        return httpx.Response(204)

    result = _client(handler).job_started(7, worker_id="policy-worker-1", started_at=NOW)
    assert result is StartResult.STARTED


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("JOB_ALREADY_PROCESSING", StartResult.ALREADY_PROCESSING),
        ("JOB_ALREADY_FINISHED", StartResult.ALREADY_FINISHED),
    ],
)
def test_job_started_409_code_decides_result(
    code: str, expected: StartResult, caplog: pytest.LogCaptureFixture
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(409, json={"code": code, "message": "x", "details": {}})

    with caplog.at_level(logging.INFO, logger="app.integrations.spring_client"):
        result = _client(handler).job_started(7, worker_id="policy-worker-1", started_at=NOW)
    assert result is expected
    assert calls["n"] == 1  # 409는 재시도하지 않는다
    records = [r for r in caplog.records if r.name == "app.integrations.spring_client"]
    assert [(r.levelno, r.getMessage()) for r in records] == [
        (logging.INFO, f"job 7 not started: {code}")
    ]


@pytest.mark.parametrize("code", ["INVALID_STATE", "DUPLICATE_REQUEST"])
def test_job_started_raises_409_with_other_code(code: str) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(409, json={"code": code, "message": "x"})

    with pytest.raises(SpringApiError) as excinfo:
        _client(handler).job_started(7, worker_id="policy-worker-1", started_at=NOW)
    assert (excinfo.value.status, excinfo.value.code) == (409, code)
    assert calls["n"] == 1


def test_job_started_raises_other_errors() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, json={"code": "INTERNAL_ERROR", "message": "boom"})

    sleeps: list[float] = []
    with pytest.raises(SpringApiError) as excinfo:
        _client(handler, sleeps).job_started(7, worker_id="policy-worker-1", started_at=NOW)
    assert (excinfo.value.status, excinfo.value.code) == (500, "INTERNAL_ERROR")
    assert calls["n"] == 3 and len(sleeps) == 2  # 5xx는 기존 재시도 정책을 그대로 따른다


@pytest.mark.parametrize(
    ("code", "retryable"), [("EMBEDDING_FAILED", True), ("HASH_MISMATCH", False)]
)
def test_job_failed_sets_retryable_from_code(code: Any, retryable: bool) -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(204)

    _client(handler).job_failed(7, error_code=code, message="x" * 900)
    assert bodies[0]["errorCode"] == code
    assert bodies[0]["retryable"] is retryable
    assert len(bodies[0]["message"]) == 500


def test_5xx_is_retried_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] == 1 else httpx.Response(204)

    sleeps: list[float] = []
    _client(handler, sleeps).policy_ingested(12, _report())
    assert calls["n"] == 2 and len(sleeps) == 1


def test_4xx_raises_with_error_envelope_code_and_no_retry() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, json={"code": "RESOURCE_NOT_FOUND", "message": "no doc"})

    with pytest.raises(SpringApiError) as excinfo:
        _client(handler).policy_ingested(99, _report())
    assert (excinfo.value.status, excinfo.value.code, calls["n"]) == (404, "RESOURCE_NOT_FOUND", 1)
    assert KEY not in str(excinfo.value)


def test_unconfigured_client_blocks() -> None:
    with pytest.raises(ContractBlockedError):
        SpringInternalClient(base_url="", api_key="")


def _entry(**overrides: Any) -> SourceEntry:
    base: dict[str, Any] = {
        "path": "visa/selection/e74_selection_20260601.pdf",
        "domain": "visa",
        "sha256": "a" * 64,
        "policy_document_id": 12,
        "document_version": "E74-2026-v1",
        "review": {"verified": True},
    }
    base.update(overrides)
    return SourceEntry.model_validate(base)


def test_manifest_gateway_uses_only_verified_entries_and_blocks_search() -> None:
    gateway = ManifestPolicyGateway(
        [
            _entry(),
            _entry(policy_document_id=13, review={"verified": False}),
            _entry(policy_document_id=None),
        ]
    )
    record = gateway.get_version(12, "E74-2026-v1")
    assert (record.content_hash, record.review_status, record.searchable) == (
        "a" * 64,
        "DRAFT",
        False,
    )
    with pytest.raises(LookupError):
        gateway.get_version(13, "E74-2026-v1")
    with pytest.raises(ContractBlockedError):
        gateway.current_versions([12])
    gateway.record_r2_object(12, "E74-2026-v1", "k")
    assert gateway.r2_keys == {(12, "E74-2026-v1"): "k"}


def test_ingested_report_only_for_stored_documents() -> None:
    entry = _entry()
    loaded = SourceResult(entry.path, "visa", "loaded", chunks=41)
    body = ingested_report(
        entry,
        loaded,
        object_key=None,
        mongo_collection="visa_policy_chunks",
        embedding_model="BAAI/bge-m3",
        embedding_revision="5617a9f6",
        now=NOW,
    ).to_json()
    assert (body["chunkCount"], body["contentHash"], body["objectKey"]) == (41, "a" * 64, None)

    from app.rag.errors import ValidationFailedError

    with pytest.raises(ValidationFailedError):
        ingested_report(
            entry,
            SourceResult(entry.path, "visa", "blocked"),
            object_key=None,
            mongo_collection="c",
            embedding_model="m",
            embedding_revision="r",
            now=NOW,
        )


DETAIL = {
    "policyDocumentId": 42,
    "sourceType": "LAW",
    "title": "출입국관리법",
    "issuingAgency": "법무부",
    "sourceUrl": "https://www.law.go.kr/x",
    "externalId": "000001",
    "visaTypeCode": None,
    "publishedOn": "2026-01-01",
    "effectiveFrom": "2026-07-01",
    "effectiveTo": None,
    "policyVersion": "LAW-000001-20260701",
    "contentHash": "a" * 64,
    "objectKey": "policy-sources/visa/sha256/" + "a" * 64 + ".xml",
    "reviewStatus": "DRAFT",
}


def test_policy_document_gets_detail_with_token_and_request_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/internal/v1/ai-context/policy-documents/42"
        assert request.headers["X-Internal-Token"] == KEY
        assert request.headers["X-Request-Id"] == "req_abc"
        return httpx.Response(200, json=DETAIL)

    detail = _client(handler).policy_document(42, request_id="req_abc")
    assert detail.policy_document_id == 42
    assert detail.policy_version == "LAW-000001-20260701"
    assert detail.object_key == DETAIL["objectKey"]
    assert str(detail.effective_from) == "2026-07-01" and detail.effective_to is None


def test_policy_document_does_not_retry_and_raises_status() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, json={"code": "INTERNAL_ERROR"})

    with pytest.raises(SpringApiError) as excinfo:
        _client(handler).policy_document(42)
    assert (excinfo.value.status, calls["n"]) == (503, 1)  # 재시도는 Worker retry 큐가 한다


def test_policy_document_rejects_unexpected_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"policyDocumentId": 42})

    with pytest.raises(SpringApiError) as excinfo:
        _client(handler).policy_document(42)
    assert (excinfo.value.status, excinfo.value.code) == (200, "INVALID_BODY")

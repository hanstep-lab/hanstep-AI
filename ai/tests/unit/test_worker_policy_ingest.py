"""POLICY_INGEST 처리: 문서 조회 → R2 → 해시 대조 → 추출 → 저장 → INT-13, 단계별 실패 코드."""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pymongo.errors import OperationFailure, ServerSelectionTimeoutError

from app.integrations.policy_gateway import UnconfiguredPolicyGateway
from app.integrations.r2 import InMemoryObjectStore, object_key
from app.integrations.spring_client import (
    PolicyDocumentDetail,
    PolicyIngestedReport,
    SpringApiError,
)
from app.rag.chunking import approx_token_count
from app.rag.ingestion import IngestionDeps
from app.worker.handler import JobFailure, JobMessage
from app.worker.policy_ingest import PolicyIngestDeps, PolicyIngestProcessor
from tests.conftest import DATA_ROOT, FIXTURES, FakeEmbedder, FakeRepository

pytestmark = pytest.mark.unit

LAW_XML = (FIXTURES / "law_api" / "lawService_900001.xml").read_bytes()
LAW_SHA = hashlib.sha256(LAW_XML).hexdigest()
LAW_KEY = object_key("policy-sources", "visa", LAW_SHA, ".xml")
SOURCE_URL = "https://www.law.go.kr/lsInfoP.do?lsiSeq=900001"
MESSAGE = JobMessage.model_validate(
    {
        "schemaVersion": 1,
        "jobId": 7,
        "jobType": "POLICY_INGEST",
        "resourceType": "POLICY_DOCUMENT",
        "resourceId": 42,
        "requestId": "req_abc",
    }
)


def _detail(**overrides: Any) -> PolicyDocumentDetail:
    base: dict[str, Any] = {
        "policyDocumentId": 42,
        "sourceType": "LAW",
        "title": "출입국관리법",
        "issuingAgency": "법무부",
        "sourceUrl": SOURCE_URL,
        "externalId": "000001",
        "visaTypeCode": "E-7-4",
        "publishedOn": "2026-01-01",
        "effectiveFrom": "2026-07-01",
        "effectiveTo": None,
        "policyVersion": "LAW-000001-20260701",
        "contentHash": LAW_SHA,
        "objectKey": LAW_KEY,
        "reviewStatus": "DRAFT",
    }
    base.update(overrides)
    return PolicyDocumentDetail.model_validate(base)


class FakeSpringDocs:
    def __init__(
        self,
        detail: PolicyDocumentDetail,
        *,
        lookup_error: Exception | None = None,
        callback_error: Exception | None = None,
    ) -> None:
        self.detail = detail
        self.lookup_error = lookup_error
        self.callback_error = callback_error
        self.reports: list[tuple[int, PolicyIngestedReport, str | None]] = []

    def policy_document(
        self, policy_document_id: int, *, timeout: float = 10.0, request_id: str | None = None
    ) -> PolicyDocumentDetail:
        assert policy_document_id == 42
        if self.lookup_error:
            raise self.lookup_error
        return self.detail

    def policy_ingested(
        self,
        policy_document_id: int,
        report: PolicyIngestedReport,
        *,
        request_id: str | None = None,
    ) -> None:
        if self.callback_error:
            raise self.callback_error
        self.reports.append((policy_document_id, report, request_id))


class BrokenStore(InMemoryObjectStore):
    def read_bytes(self, key: str) -> bytes:
        raise ConnectionError("R2 unavailable: EndpointConnectionError")


class MongoDownRepository(FakeRepository):
    def existing_chunks(self, pid: int, version: str, model: str) -> list[Any]:
        raise ServerSelectionTimeoutError("mongodb:27017: timed out")


def _processor(
    spring: FakeSpringDocs,
    *,
    store: InMemoryObjectStore | None = None,
    repo: FakeRepository | None = None,
    embedder: FakeEmbedder | None = None,
) -> PolicyIngestProcessor:
    if store is None:
        store = InMemoryObjectStore()
        store.objects[LAW_KEY] = LAW_XML
    ingestion = IngestionDeps(
        repository=repo or FakeRepository(),  # type: ignore[arg-type]
        gateway=UnconfiguredPolicyGateway(),
        object_stores={"visa": store},
        embedder=embedder or FakeEmbedder(),
        token_counter=approx_token_count,
        tokenizer_id="approx",
        r2_prefix="policy-sources",
        embedding_model="BAAI/bge-m3",
    )
    return PolicyIngestProcessor(
        PolicyIngestDeps(
            spring=spring,
            object_store=store,
            ingestion=ingestion,
            mongo_collection="visa_policy_chunks",
            embedding_revision="5617a9f61b028005a4858fdac845db406aefb181",
            clock=lambda: datetime(2026, 11, 16, 12, 0, tzinfo=UTC),
        )
    )


def _failure(processor: PolicyIngestProcessor) -> JobFailure:
    with pytest.raises(JobFailure) as excinfo:
        processor(MESSAGE)
    return excinfo.value


def test_ingests_law_xml_from_r2_and_reports_int13(caplog: pytest.LogCaptureFixture) -> None:
    spring, repo, embedder = FakeSpringDocs(_detail()), FakeRepository(), FakeEmbedder()

    with caplog.at_level(logging.INFO, logger="app.worker.policy_ingest"):
        _processor(spring, repo=repo, embedder=embedder)(MESSAGE)

    stored = repo.load_version(42, "LAW-000001-20260701", "BAAI/bge-m3")
    assert stored and all(r.review_status == "pending" and not r.is_active for r in stored)
    assert {r.source_type for r in stored} == {"law"}
    assert {r.source_sha256 for r in stored} == {LAW_SHA}
    assert stored[0].visa_types == ["E-7-4"]
    assert stored[0].source_url == SOURCE_URL
    [(pid, report, request_id)] = spring.reports
    assert (pid, request_id) == (42, "req_abc")
    assert report.document_version == "LAW-000001-20260701"
    assert report.content_hash == LAW_SHA
    assert report.chunk_count == len(stored)
    assert report.object_key == LAW_KEY
    assert report.mongo_collection == "visa_policy_chunks"
    assert embedder.embedded_texts == len(stored)
    # 로그에는 단계별 시간만 남고 원문 URL·제목은 없다
    [line] = [r.getMessage() for r in caplog.records]
    for stage in ("lookup_ms", "fetch_ms", "extract_ms", "embed_ms", "store_ms", "callback_ms"):
        assert stage in line
    assert "jobId=7 requestId=req_abc" in line
    assert SOURCE_URL not in line and "출입국관리법" not in line


def test_reprocessing_same_document_embeds_nothing_and_reports_again() -> None:
    spring, repo, embedder = FakeSpringDocs(_detail()), FakeRepository(), FakeEmbedder()
    processor = _processor(spring, repo=repo, embedder=embedder)
    processor(MESSAGE)
    embedded = embedder.embedded_texts

    processor(MESSAGE)  # 죽은 뒤 재전달: 청크는 이미 다 있음

    assert embedder.embedded_texts == embedded
    assert len(spring.reports) == 2
    assert spring.reports[1][1].chunk_count == spring.reports[0][1].chunk_count


def test_pdf_notice_is_extracted_by_file_kind() -> None:
    path = "visa/recommendations/central/mafra/e74_mafra_20260305.pdf"
    data = (DATA_ROOT / path).read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    key = object_key("policy-sources", "visa", sha, ".pdf")
    store = InMemoryObjectStore()
    store.objects[key] = data
    spring = FakeSpringDocs(_detail(sourceType="NOTICE", contentHash=sha, objectKey=key))

    _processor(spring, store=store)(MESSAGE)

    assert spring.reports[0][1].chunk_count > 0


def test_document_without_object_key_is_source_missing() -> None:
    failure = _failure(_processor(FakeSpringDocs(_detail(objectKey=None))))
    assert (failure.code, failure.transient) == ("SOURCE_MISSING", False)


def test_r2_object_not_found_is_source_missing() -> None:
    failure = _failure(_processor(FakeSpringDocs(_detail()), store=InMemoryObjectStore()))
    assert (failure.code, failure.transient) == ("SOURCE_MISSING", False)


def test_r2_connection_error_is_transient() -> None:
    failure = _failure(_processor(FakeSpringDocs(_detail()), store=BrokenStore()))
    assert (failure.code, failure.transient) == ("SOURCE_FETCH_FAILED", True)
    assert "http" not in failure.reason


def test_r2_bytes_differing_from_content_hash_is_hash_mismatch() -> None:
    failure = _failure(_processor(FakeSpringDocs(_detail(contentHash="0" * 64))))
    assert (failure.code, failure.transient) == ("HASH_MISMATCH", False)


def test_unreadable_source_is_extraction_failed() -> None:
    data = b"plain text, not a supported document"
    sha = hashlib.sha256(data).hexdigest()
    key = object_key("policy-sources", "visa", sha, ".txt")
    store = InMemoryObjectStore()
    store.objects[key] = data
    spring = FakeSpringDocs(_detail(sourceType="NOTICE", contentHash=sha, objectKey=key))

    failure = _failure(_processor(spring, store=store))

    assert (failure.code, failure.transient) == ("EXTRACTION_FAILED", False)


@pytest.mark.parametrize(
    ("error", "code", "transient"),
    [
        (httpx.ConnectTimeout("timed out"), "SPRING_UNAVAILABLE", True),
        (SpringApiError(503, "INTERNAL_ERROR", ""), "SPRING_UNAVAILABLE", True),
        (SpringApiError(404, "RESOURCE_NOT_FOUND", ""), "SPRING_REJECTED", False),
        (SpringApiError(200, "INVALID_BODY", ""), "SPRING_REJECTED", False),
    ],
)
def test_lookup_errors(error: Exception, code: str, transient: bool) -> None:
    failure = _failure(_processor(FakeSpringDocs(_detail(), lookup_error=error)))
    assert (failure.code, failure.transient) == (code, transient)


def test_mongodb_connection_error_is_transient_vector_store_failure() -> None:
    failure = _failure(_processor(FakeSpringDocs(_detail()), repo=MongoDownRepository()))
    assert (failure.code, failure.transient) == ("VECTOR_STORE_FAILED", True)


def test_mongodb_command_error_is_not_transient() -> None:
    class Rejecting(FakeRepository):
        def existing_chunks(self, pid: int, version: str, model: str) -> list[Any]:
            raise OperationFailure("not authorized")

    failure = _failure(_processor(FakeSpringDocs(_detail()), repo=Rejecting()))
    assert (failure.code, failure.transient) == ("VECTOR_STORE_FAILED", False)


def test_callback_5xx_is_transient_and_retry_embeds_nothing() -> None:
    spring, repo, embedder = (
        FakeSpringDocs(_detail(), callback_error=SpringApiError(0, "TRANSPORT_ERROR", "")),
        FakeRepository(),
        FakeEmbedder(),
    )
    processor = _processor(spring, repo=repo, embedder=embedder)

    failure = _failure(processor)
    assert (failure.code, failure.transient) == ("SPRING_UNAVAILABLE", True)
    embedded = embedder.embedded_texts

    spring.callback_error = None
    processor(MESSAGE)  # retry 큐에서 돌아온 메시지

    assert embedder.embedded_texts == embedded
    assert len(spring.reports) == 1


def test_callback_409_is_rejected() -> None:
    error = SpringApiError(409, "INVALID_STATE", "documentVersion does not match")
    failure = _failure(_processor(FakeSpringDocs(_detail(), callback_error=error)))
    assert (failure.code, failure.transient) == ("SPRING_REJECTED", False)


def test_document_without_visa_type_fails_before_embedding() -> None:
    embedder = FakeEmbedder()
    processor = _processor(FakeSpringDocs(_detail(visaTypeCode=None)), embedder=embedder)

    failure = _failure(processor)

    assert (failure.code, failure.transient) == ("METADATA_INVALID", False)
    assert failure.reason == "metadata: visa_types"
    assert embedder.document_calls == 0  # 임베딩 전에 막는다

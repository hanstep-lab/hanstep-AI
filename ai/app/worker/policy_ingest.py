"""POLICY_INGEST 처리: 문서 조회 → R2 원본 → 해시 대조 → 추출·청킹 → 임베딩·저장 → INT-13.

추출 뒤의 청킹 통계·저장·INT-13 본문은 스크립트 적재와 같은 함수를 쓴다(app.rag.ingestion).
실패는 단계별 코드로 :class:`JobFailure`를 던진다. 연결 오류만 일시 오류(transient)다.
로그에는 jobId·requestId·단계별 시간만 남기고 문서 본문·URL은 넣지 않는다.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic
from typing import Protocol

from pydantic import ValidationError
from pymongo.errors import ConnectionFailure, PyMongoError

from app.integrations.r2 import ObjectStore
from app.integrations.spring_client import (
    PolicyDocumentDetail,
    PolicyFailureCode,
    PolicyIngestedReport,
)
from app.rag.chunking import ChunkDraft, chunk_blocks
from app.rag.errors import RagError, ValidationFailedError, VersionConflictError
from app.rag.ingestion import (
    SEOUL,
    IngestionDeps,
    SourceResult,
    build_records,
    extract_bytes,
    ingested_report,
    record_chunks,
    store_chunks,
)
from app.rag.manifest import SourceEntry
from app.rag.schemas import EMBEDDING_DIMENSIONS
from app.worker.handler import JobFailure, JobMessage, spring_failure

logger = logging.getLogger(__name__)

# policy_documents는 모두 비자 정책 문서다(R2 정책 버킷, visa_policy_chunks).
DOMAIN = "visa"
# 메타 사전 검사용 벡터(0 벡터는 거부되므로 첫 값만 1). 저장하지 않는다.
_PROBE_VECTOR = [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1)

# 추출 상태별 실패 코드. 모두 재시도해도 결과가 같다.
_EXTRACTION_FAILURES: dict[str, PolicyFailureCode] = {
    "needs_ocr": "NEEDS_OCR",
    "needs_conversion": "NEEDS_CONVERSION",
    "failed": "EXTRACTION_FAILED",
}


class PolicyDocumentSource(Protocol):
    """SpringInternalClient 중 문서 조회와 적재 완료 보고."""

    def policy_document(
        self, policy_document_id: int, *, timeout: float = 10.0, request_id: str | None = None
    ) -> PolicyDocumentDetail: ...

    def policy_ingested(
        self,
        policy_document_id: int,
        report: PolicyIngestedReport,
        *,
        request_id: str | None = None,
    ) -> None: ...


@dataclass
class PolicyIngestDeps:
    spring: PolicyDocumentSource
    object_store: ObjectStore
    ingestion: IngestionDeps
    mongo_collection: str
    embedding_revision: str
    # 모델을 시작 때 올렸는지(로그용)
    model_loaded: Callable[[], bool] = lambda: False
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(SEOUL))


def source_entry(detail: PolicyDocumentDetail, object_key: str) -> SourceEntry:
    """BE 문서 단건을 스크립트 적재와 같은 입력(SourceEntry)으로 바꾼다."""
    return SourceEntry.model_validate(
        {
            "path": object_key,
            "domain": DOMAIN,
            "sha256": detail.content_hash,
            "title": detail.title,
            "source_url": detail.source_url,
            "source_org": detail.issuing_agency,
            "visa_types": [detail.visa_type_code] if detail.visa_type_code else None,
            "source_type": detail.source_type.lower(),
            "issued_on": detail.published_on,
            "policy_document_id": detail.policy_document_id,
            "document_version": detail.policy_version,
            "effective_from": detail.effective_from,
            "effective_to": detail.effective_to,
        }
    )


def check_metadata(entry: SourceEntry, draft: ChunkDraft, deps: PolicyIngestDeps) -> None:
    """청크 한 개를 미리 만들어 문서 메타가 저장 조건(체류자격 등)에 맞는지 임베딩 전에 본다.

    맞지 않으면 임베딩(문서당 수 분)을 하고 나서야 실패하므로 여기서 먼저 막는다.
    """
    try:
        build_records(
            entry,
            [draft],
            {draft.chunk_index: _PROBE_VECTOR},
            embedding_model=deps.ingestion.embedding_model,
            now=deps.clock(),
        )
    except (ValidationError, ValidationFailedError) as exc:
        raise JobFailure(
            "METADATA_INVALID", transient=False, reason=f"metadata: {_invalid_fields(exc)}"
        ) from exc


def _invalid_fields(exc: Exception) -> str:
    # ValidationError 문자열에는 입력값(본문)이 들어가므로 필드 이름만 남긴다
    if isinstance(exc, ValidationError):
        return ",".join(".".join(str(p) for p in e["loc"]) for e in exc.errors())
    return type(exc).__name__


def store_failure(exc: Exception) -> JobFailure:
    """임베딩·MongoDB 저장 단계 오류 분류. MongoDB 연결 오류만 일시 오류다."""
    name = type(exc).__name__
    if isinstance(exc, ValidationError):
        return JobFailure(
            "METADATA_INVALID", transient=False, reason=f"metadata: {_invalid_fields(exc)}"
        )
    if isinstance(exc, ConnectionFailure):
        return JobFailure("VECTOR_STORE_FAILED", transient=True, reason=f"store: {name}")
    if isinstance(exc, VersionConflictError):
        return JobFailure("VERSION_CONFLICT", transient=False, reason="store: version conflict")
    if isinstance(exc, (PyMongoError, ValidationFailedError)):
        return JobFailure("VECTOR_STORE_FAILED", transient=False, reason=f"store: {name}")
    return JobFailure("EMBEDDING_FAILED", transient=False, reason=f"embed: {name}")


class PolicyIngestProcessor:
    """작업 메시지 하나를 적재한다. 다시 처리하면 저장된 청크는 다시 임베딩하지 않는다."""

    def __init__(self, deps: PolicyIngestDeps) -> None:
        self._deps = deps

    def __call__(self, message: JobMessage) -> None:
        deps = self._deps
        tag = f"jobId={message.job_id} requestId={message.request_id}"
        timings: dict[str, int] = {}

        with _timed(timings, "lookup_ms"):
            try:
                detail = deps.spring.policy_document(
                    message.resource_id, request_id=message.request_id
                )
            except Exception as exc:
                failure = spring_failure(exc, "lookup")
                if failure is None:
                    raise
                raise failure from exc
        if not detail.object_key:
            raise JobFailure("SOURCE_MISSING", transient=False, reason="document has no object key")

        with _timed(timings, "fetch_ms"):
            try:
                data = deps.object_store.read_bytes(detail.object_key)
            except LookupError as exc:
                raise JobFailure(
                    "SOURCE_MISSING", transient=False, reason="R2 object not found"
                ) from exc
            except ConnectionError as exc:
                raise JobFailure(
                    "SOURCE_FETCH_FAILED", transient=True, reason=f"fetch: {exc}"
                ) from exc
        if hashlib.sha256(data).hexdigest() != detail.content_hash:
            raise JobFailure(
                "HASH_MISMATCH", transient=False, reason="R2 bytes differ from content_hash"
            )

        entry = source_entry(detail, detail.object_key)
        result = SourceResult(entry.path, entry.domain, "planned", sha256=entry.sha256)
        with _timed(timings, "extract_ms"):
            report = extract_bytes(data, entry.source_type)
            if report.status != "ok":
                raise JobFailure(
                    _EXTRACTION_FAILURES[report.status],
                    transient=False,
                    reason=f"extract: {report.status} {report.extractor}",
                )
            try:
                drafts = chunk_blocks(
                    report.blocks, deps.ingestion.token_counter, deps.ingestion.chunker
                )
            except RagError as exc:
                raise JobFailure(
                    "EXTRACTION_FAILED", transient=False, reason=f"chunk: {type(exc).__name__}"
                ) from exc
            record_chunks(result, drafts, report, deps.ingestion)
            if not drafts:
                raise JobFailure("EXTRACTION_FAILED", transient=False, reason="no chunks produced")
        check_metadata(entry, drafts[0], deps)

        try:
            store_chunks(entry, drafts, deps.ingestion, result)
        except Exception as exc:
            raise store_failure(exc) from exc
        timings.update(result.timings)

        body = ingested_report(
            entry,
            result,
            object_key=detail.object_key,
            mongo_collection=deps.mongo_collection,
            embedding_model=deps.ingestion.embedding_model,
            embedding_revision=deps.embedding_revision,
            now=deps.clock(),
        )
        with _timed(timings, "callback_ms"):
            try:
                deps.spring.policy_ingested(
                    detail.policy_document_id, body, request_id=message.request_id
                )
            except Exception as exc:
                failure = spring_failure(exc, "callback")
                if failure is None:
                    raise
                raise failure from exc

        logger.info(
            "%s policyDocumentId=%s version=%s %s chunks=%d embedded=%d inserted=%d "
            "modelLoaded=%s timings=%s",
            tag,
            detail.policy_document_id,
            detail.policy_version,
            result.status,
            result.chunks,
            result.embedded,
            result.inserted,
            deps.model_loaded(),
            timings,
        )


@contextmanager
def _timed(timings: dict[str, int], name: str) -> Iterator[None]:
    started = monotonic()
    try:
        yield
    finally:
        timings[name] = int((monotonic() - started) * 1000)

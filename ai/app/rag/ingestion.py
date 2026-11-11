"""수동 문서 적재: preflight -> 추출 -> 청킹 -> (R2, MySQL 확인, 임베딩, upsert).

새 청크는 항상 ``pending``/비활성 상태로 기록한다. 활성화는 MySQL 승인 뒤
:mod:`app.rag.publication`에서만 일어난다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from app.integrations.policy_gateway import PolicyDocumentGateway
from app.integrations.r2 import ObjectStore, object_key
from app.integrations.spring_client import PolicyIngestedReport
from app.rag.chunking import ChunkDraft, ChunkerConfig, TokenCounter, chunk_blocks, fingerprint
from app.rag.embeddings import Embedder
from app.rag.errors import ContractBlockedError, RagError, ValidationFailedError
from app.rag.extractors import ExtractionReport, extract, extract_derived
from app.rag.manifest import SourceEntry, preflight
from app.rag.repository import ChunkRepository, plan_version_write
from app.rag.schemas import ChunkRecord, utc_now
from app.rag.sources import inspect_file, resolve_inside

logger = logging.getLogger(__name__)
SEOUL = ZoneInfo("Asia/Seoul")

ResultStatus = Literal[
    "planned", "loaded", "skipped", "blocked", "needs_conversion", "needs_ocr", "failed"
]


@dataclass
class SourceResult:
    """매니페스트 소스 항목 하나의 적재 처리 결과."""

    path: str
    domain: str
    status: ResultStatus
    reasons: list[str] = field(default_factory=list)
    sha256: str = ""
    extraction: dict[str, object] = field(default_factory=dict)
    chunks: int = 0
    max_chunk_tokens: int = 0
    chunks_with_page: int = 0
    embedded: int = 0
    inserted: int = 0
    already_present: int = 0
    r2: str = ""
    chunk_fingerprint: str = ""
    sample_chunks: list[dict[str, object]] = field(default_factory=list)
    # INT-13 전달 결과: "", "reported" 또는 "error: ..."
    spring_report: str = ""


@dataclass
class IngestionDeps:
    """적재 파이프라인에 주입하는 저장소·연동·임베딩 의존성 묶음."""

    repository: ChunkRepository | None
    gateway: PolicyDocumentGateway
    # 도메인별: visa -> 정책 버킷, finance -> 상품 버킷.
    object_stores: Mapping[str, ObjectStore]
    embedder: Embedder | None
    token_counter: TokenCounter
    tokenizer_id: str
    r2_prefix: str
    embedding_model: str
    chunker: ChunkerConfig = field(default_factory=ChunkerConfig)
    clock: Callable[[], datetime] = utc_now


def seoul_midnight_utc(day: date) -> datetime:
    """시행일은 한국 달력 날짜다. KST 자정을 UTC Date로 저장한다."""
    return datetime.combine(day, time.min, tzinfo=SEOUL).astimezone(UTC)


def _extract_entry(entry: SourceEntry, root: Path) -> tuple[ExtractionReport, bytes]:
    original = resolve_inside(root, entry.path).read_bytes()
    found = inspect_file(root, entry.path)
    if entry.derived_inputs:
        derived = entry.derived_inputs[0]
        derived_info = inspect_file(root, derived.path)
        data = resolve_inside(root, derived.path).read_bytes()
        report = extract_derived(derived_info.detected_kind, data)
        return report, original
    return extract(found.detected_kind, original), original


def build_records(
    entry: SourceEntry,
    drafts: Sequence[ChunkDraft],
    vectors: dict[int, list[float]],
    *,
    embedding_model: str,
    now: datetime,
) -> list[ChunkRecord]:
    source_type = entry.source_type
    if entry.policy_document_id is None or not entry.document_version or source_type is None:
        raise ValidationFailedError("policy_document_id/document_version/source_type unconfirmed")
    records: list[ChunkRecord] = []
    for draft in drafts:
        if draft.chunk_index not in vectors:
            continue
        records.append(
            ChunkRecord(
                policy_document_id=entry.policy_document_id,
                document_version=entry.document_version,
                chunk_index=draft.chunk_index,
                chunk_text=draft.text,
                embedding=vectors[draft.chunk_index],
                embedding_model=embedding_model,
                source_sha256=entry.sha256,
                title=entry.title or "",
                source_url=entry.source_url or "",
                source_org=entry.source_org or "",
                visa_types=list(entry.visa_types or []),
                source_type=source_type,
                language="ko",
                review_status="pending",
                is_active=False,
                created_at=now,
                updated_at=now,
                page_number=draft.page_number,
                section_title=draft.section_title,
                issued_at=seoul_midnight_utc(entry.issued_on) if entry.issued_on else None,
                effective_from=seoul_midnight_utc(entry.effective_from)
                if entry.effective_from
                else None,
                effective_to=seoul_midnight_utc(entry.effective_to) if entry.effective_to else None,
            )
        )
    return records


def process_entry(
    entry: SourceEntry, root: Path, deps: IngestionDeps, *, apply: bool
) -> SourceResult:
    found, blockers = preflight(entry, root)
    result = SourceResult(entry.path, entry.domain, "planned", sha256=found.sha256)
    if found.status != "found":
        result.status, result.reasons = "failed", blockers
        return result

    report, original = _extract_entry(entry, root)
    result.extraction = report.summary()
    if report.status != "ok":
        result.status = report.status
        result.reasons = [report.reason, report.remedy, *blockers]
        return result

    try:
        drafts = chunk_blocks(report.blocks, deps.token_counter, deps.chunker)
    except RagError as exc:
        result.status, result.reasons = "failed", [f"chunking failed: {exc}", *blockers]
        return result
    result.chunks = len(drafts)
    result.max_chunk_tokens = max((d.token_count for d in drafts), default=0)
    result.chunks_with_page = sum(1 for d in drafts if d.page_number is not None)
    result.chunk_fingerprint = fingerprint(deps.chunker, report.extractor, deps.tokenizer_id)
    result.sample_chunks = [
        {
            "chunk_index": d.chunk_index,
            "page_number": d.page_number,
            "section_title": d.section_title,
            "tokens": d.token_count,
            "repeated_table_header": d.repeated_table_header,
            "preview": d.text[:120],
        }
        for d in drafts[:2]
    ]
    if not drafts:
        result.status, result.reasons = "failed", ["no chunks produced", *blockers]
        return result
    if not apply:
        result.reasons = blockers
        return result
    if blockers:
        result.status, result.reasons = "blocked", blockers
        return result

    try:
        return _apply(entry, drafts, original, deps, result)
    except ContractBlockedError as exc:
        result.status, result.reasons = "blocked", [str(exc)]
    except (RagError, LookupError, ConnectionError) as exc:
        result.status, result.reasons = "failed", [f"{type(exc).__name__}: {exc}"]
    return result


def _apply(
    entry: SourceEntry,
    drafts: Sequence[ChunkDraft],
    original: bytes,
    deps: IngestionDeps,
    result: SourceResult,
) -> SourceResult:
    assert entry.policy_document_id is not None and entry.document_version is not None
    if deps.repository is None or deps.embedder is None:
        raise ContractBlockedError("MongoDB and embedding model are required for --apply")

    # 1. MySQL이 기준 원본이다. 해당 버전이 이 해시로 이미 등록돼 있어야 한다.
    record = deps.gateway.get_version(entry.policy_document_id, entry.document_version)
    if record.content_hash != entry.sha256:
        raise ValidationFailedError("MySQL source_sha256 differs from the file on disk")

    # 2. 원본을 비공개 R2에 보관하고 왕복(round trip)을 검증한다.
    key = object_key(deps.r2_prefix, entry.domain, entry.sha256, Path(entry.path).suffix)
    store = deps.object_stores.get(entry.domain)
    if store is None:
        raise ContractBlockedError(f"no R2 bucket configured for domain {entry.domain}")
    result.r2 = store.put_if_absent(key, original, entry.sha256)
    if store.read_sha256(key) != entry.sha256:
        raise ValidationFailedError("R2 round-trip hash mismatch")
    if record.object_key != key:
        deps.gateway.record_r2_object(entry.policy_document_id, entry.document_version, key)

    # 3. 임베딩 호출을 쓰기 전에 noop/resume/insert를 결정한다.
    repo = deps.repository
    existing = repo.existing_chunks(
        entry.policy_document_id, entry.document_version, deps.embedding_model
    )
    plan = plan_version_write(
        existing, source_sha256=entry.sha256, expected_texts=[d.text for d in drafts]
    )
    if plan.action == "noop":
        result.status = "skipped"
        result.already_present = len(existing)
        result.reasons = ["same version and hash already stored; approval state preserved"]
        return result

    todo = [d for d in drafts if d.chunk_index in set(plan.missing_indexes)]
    vectors = deps.embedder.embed_documents([d.text for d in todo])
    result.embedded = len(vectors)
    records = build_records(
        entry,
        todo,
        {d.chunk_index: v for d, v in zip(todo, vectors, strict=True)},
        embedding_model=deps.embedding_model,
        now=deps.clock(),
    )
    write = repo.insert_chunks(records)
    result.inserted, result.already_present = write.inserted, write.already_present
    if write.errors:
        raise ValidationFailedError("; ".join(write.errors[:5]))

    # 4. 다시 읽어 id/version/hash/count/text를 검증한다.
    stored = repo.load_version(
        entry.policy_document_id, entry.document_version, deps.embedding_model
    )
    if len(stored) != len(drafts):
        raise ValidationFailedError(f"stored {len(stored)} chunks, expected {len(drafts)}")
    for draft, chunk in zip(drafts, stored, strict=True):
        if chunk.chunk_index != draft.chunk_index or chunk.chunk_text != draft.text:
            raise ValidationFailedError(f"stored chunk {chunk.chunk_index} does not match")
        if chunk.source_sha256 != entry.sha256:
            raise ValidationFailedError("stored source_sha256 mismatch")
    result.status = "loaded"
    return result


def ingested_report(
    entry: SourceEntry,
    result: SourceResult,
    *,
    object_key: str | None,
    mongo_collection: str,
    embedding_model: str,
    embedding_revision: str,
    now: datetime,
) -> PolicyIngestedReport:
    """청크가 모두 저장된(loaded 또는 이미 존재) 문서의 INT-13 본문."""
    if result.status not in ("loaded", "skipped") or entry.document_version is None:
        raise ValidationFailedError(f"{entry.path} is not fully ingested ({result.status})")
    return PolicyIngestedReport(
        document_version=entry.document_version,
        content_hash=entry.sha256,
        chunk_count=result.chunks,
        mongo_collection=mongo_collection,
        embedding_model=embedding_model,
        embedding_revision=embedding_revision,
        object_key=object_key,
        ingested_at=now,
    )

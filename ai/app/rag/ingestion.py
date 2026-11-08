"""Manual-document ingestion: preflight -> extract -> chunk -> (R2, MySQL check, embed, upsert).

New chunks are always written as ``pending``/inactive. Activation happens only in
:mod:`app.rag.publication` after MySQL approval.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from app.integrations.policy_gateway import PolicyDocumentGateway
from app.integrations.r2 import ObjectStore, object_key
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


@dataclass
class IngestionDeps:
    repository: ChunkRepository | None
    gateway: PolicyDocumentGateway
    object_store: ObjectStore
    embedder: Embedder | None
    token_counter: TokenCounter
    tokenizer_id: str
    r2_prefix: str
    embedding_model: str
    chunker: ChunkerConfig = field(default_factory=ChunkerConfig)
    clock: Callable[[], datetime] = utc_now


def seoul_midnight_utc(day: date) -> datetime:
    """Effective dates are calendar days in Korea; store the KST midnight as a UTC Date."""
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
    if entry.policy_document_id is None or not entry.document_version:
        raise ValidationFailedError("policy_document_id/document_version unconfirmed")
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
                language="ko",
                review_status="pending",
                is_active=False,
                created_at=now,
                updated_at=now,
                page_number=draft.page_number,
                section_title=draft.section_title,
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

    drafts = chunk_blocks(report.blocks, deps.token_counter, deps.chunker)
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

    # 1. MySQL is the source of truth: the version must already be registered with this hash.
    record = deps.gateway.get_version(entry.policy_document_id, entry.document_version)
    if record.source_sha256 != entry.sha256:
        raise ValidationFailedError("MySQL source_sha256 differs from the file on disk")

    # 2. Keep the original in private R2 and verify the round trip.
    key = object_key(deps.r2_prefix, entry.domain, entry.sha256, Path(entry.path).suffix)
    result.r2 = deps.object_store.put_if_absent(key, original, entry.sha256)
    if deps.object_store.read_sha256(key) != entry.sha256:
        raise ValidationFailedError("R2 round-trip hash mismatch")
    if record.r2_object_key != key:
        deps.gateway.record_r2_object(entry.policy_document_id, entry.document_version, key)

    # 3. Decide noop/resume/insert before spending any embedding calls.
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

    # 4. Read back and verify id/version/hash/count/text.
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

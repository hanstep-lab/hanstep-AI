"""Keep MongoDB chunk state in line with one MySQL ``policy_documents`` row.

BE stores every version as its own row (``content_hash`` is unique), so activation is decided
per row from that row's MySQL state; there is no "deactivate the other versions" step across
rows. MySQL and MongoDB are not one atomic unit: callers re-run the sync from the MySQL state
(approval event or periodic reconciliation). Every sync is idempotent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from bson.int64 import Int64
from pymongo import MongoClient
from pymongo.client_session import ClientSession

from app.integrations.policy_gateway import MysqlReviewStatus, PolicyDocumentRecord
from app.rag.errors import ValidationFailedError
from app.rag.repository import ChunkRepository
from app.rag.schemas import ReviewStatus, utc_now

# MySQL review_status -> (MongoDB review_status, may be active). Proposal M1 in the BE
# request; change only this table if BE settles on another mapping.
REVIEW_STATE_MAP: dict[MysqlReviewStatus, tuple[ReviewStatus, bool]] = {
    "DRAFT": ("pending", False),
    "NEEDS_REVIEW": ("pending", False),
    "APPROVED": ("approved", True),
    "RETIRED": ("approved", False),
}

# "updated": review_status changed but nothing was switched on or off.
SyncAction = Literal["activated", "deactivated", "updated", "unchanged", "not_ingested"]


@dataclass(frozen=True)
class SyncResult:
    policy_document_id: int
    policy_version: str
    action: SyncAction
    review_status: ReviewStatus
    is_active: bool
    modified: int = 0
    deactivated: int = 0
    stale_deactivated: int = 0


def target_state(record: PolicyDocumentRecord) -> tuple[ReviewStatus, bool]:
    """Active only when MySQL says APPROVED *and* BE marks the row searchable (e.g. in force)."""
    review_status, may_be_active = REVIEW_STATE_MAP[record.review_status]
    return review_status, may_be_active and record.searchable


def _transactions_supported(client: MongoClient[dict[str, Any]]) -> bool:
    hello = client.admin.command("hello")
    return bool(hello.get("setName") or hello.get("msg") == "isdbgrid")


def verify_ready(
    repo: ChunkRepository,
    record: PolicyDocumentRecord,
    embedding_model: str,
    expected_chunks: int | None,
) -> int:
    """Checks before a row may become searchable. Returns the chunk count (0 = not ingested)."""
    chunks = repo.load_version(record.policy_document_id, record.policy_version, embedding_model)
    if not chunks:
        return 0
    if [c.chunk_index for c in chunks] != list(range(len(chunks))):
        raise ValidationFailedError("chunk_index sequence is not contiguous from 0")
    if any(c.source_sha256 != record.content_hash for c in chunks):
        raise ValidationFailedError("chunk source_sha256 differs from MySQL content_hash")
    if expected_chunks is not None and len(chunks) != expected_chunks:
        raise ValidationFailedError(f"found {len(chunks)} chunks, MySQL expects {expected_chunks}")
    return len(chunks)


def sync_policy_state(
    client: MongoClient[dict[str, Any]],
    repo: ChunkRepository,
    record: PolicyDocumentRecord,
    *,
    embedding_model: str,
    expected_chunks: int | None = None,
) -> SyncResult:
    """Apply one MySQL row's state to its chunks.

    - Activation touches only the current embedding model's chunks, after verifying the full,
      contiguous chunk set matches the MySQL hash (and ``chunk_count`` when given).
    - Deactivation applies to every model's chunks of that row (fail-safe direction).
    - Chunks under the same ``policy_document_id`` with another ``document_version`` are not
      what MySQL describes for this row, so they are deactivated.
    - Activation runs in a MongoDB transaction and is refused without transaction support.
    """
    review_status, active = target_state(record)
    count = verify_ready(repo, record, embedding_model, expected_chunks) if active else 0
    pid = Int64(record.policy_document_id)

    target: dict[str, Any] = {"policy_document_id": pid, "document_version": record.policy_version}
    if active:
        target["embedding_model"] = embedding_model
    # Only touch documents whose state differs, so a repeated sync is a true no-op.
    target["$or"] = [{"review_status": {"$ne": review_status}}, {"is_active": {"$ne": active}}]
    stale = {
        "policy_document_id": pid,
        "document_version": {"$ne": record.policy_version},
        "is_active": True,
    }

    def apply(session: ClientSession | None) -> tuple[int, int, int]:
        now = utc_now()
        modified = deactivated = 0
        if not active:
            # Counted separately so a pure review_status change is not reported as deactivation.
            deactivated = repo.collection.count_documents(
                {**{k: v for k, v in target.items() if k != "$or"}, "is_active": True},
                session=session,
            )
        if not active or count:
            modified = repo.collection.update_many(
                target,
                {"$set": {"review_status": review_status, "is_active": active, "updated_at": now}},
                session=session,
            ).modified_count
        stale_count = repo.collection.update_many(
            stale, {"$set": {"is_active": False, "updated_at": now}}, session=session
        ).modified_count
        return modified, deactivated, stale_count

    if active and count:
        if not _transactions_supported(client):
            raise ValidationFailedError(
                "MongoDB deployment does not support transactions; refusing non-atomic activation"
            )
        with client.start_session() as session, session.start_transaction():
            modified, deactivated, stale_count = apply(session)
    else:
        modified, deactivated, stale_count = apply(None)

    action: SyncAction
    if active and not count:
        action = "not_ingested"
    elif active and modified:
        action = "activated"
    elif deactivated or stale_count:
        action = "deactivated"
    elif modified:
        action = "updated"
    else:
        action = "unchanged"
    return SyncResult(
        record.policy_document_id,
        record.policy_version,
        action,
        review_status,
        active and bool(count),
        modified,
        deactivated,
        stale_count,
    )

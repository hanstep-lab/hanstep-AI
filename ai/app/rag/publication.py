"""Approval-driven activation and version switch-over.

Activation requires the MySQL review state to be ``approved`` and current for exactly this
version, and a complete, verified chunk set. MongoDB activation of the new version and
deactivation of older versions run in one MongoDB transaction; MySQL and MongoDB are NOT one
atomic unit: on any failure the caller retries from the MySQL approval event; a failed
switch-over leaves the previously active version searchable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bson.int64 import Int64
from pymongo import MongoClient
from pymongo.errors import OperationFailure

from app.integrations.policy_gateway import PolicyDocumentGateway
from app.rag.errors import ValidationFailedError
from app.rag.repository import ChunkRepository
from app.rag.schemas import utc_now


@dataclass(frozen=True)
class ActivationResult:
    policy_document_id: int
    document_version: str
    activated: int
    deactivated: int


def _transactions_supported(client: MongoClient[dict[str, Any]]) -> bool:
    hello = client.admin.command("hello")
    return bool(hello.get("setName") or hello.get("msg") == "isdbgrid")


def activate_version(
    client: MongoClient[dict[str, Any]],
    repo: ChunkRepository,
    gateway: PolicyDocumentGateway,
    *,
    policy_document_id: int,
    document_version: str,
    embedding_model: str,
    expected_chunks: int,
    expected_sha256: str,
) -> ActivationResult:
    record = gateway.get_version(policy_document_id, document_version)
    if record.review_status != "approved" or not record.is_current:
        raise ValidationFailedError("MySQL does not mark this version approved and current")
    if record.source_sha256 != expected_sha256:
        raise ValidationFailedError("MySQL hash differs from the expected source hash")

    chunks = repo.load_version(policy_document_id, document_version, embedding_model)
    if len(chunks) != expected_chunks:
        raise ValidationFailedError(f"found {len(chunks)} chunks, expected {expected_chunks}")
    if [c.chunk_index for c in chunks] != list(range(expected_chunks)):
        raise ValidationFailedError("chunk_index sequence is not contiguous from 0")
    if any(c.source_sha256 != expected_sha256 for c in chunks):
        raise ValidationFailedError("chunk source_sha256 mismatch")

    if not _transactions_supported(client):
        raise ValidationFailedError(
            "MongoDB deployment does not support transactions; refusing non-atomic switch-over"
        )

    now = utc_now()
    doc_id = Int64(policy_document_id)
    with client.start_session() as session, session.start_transaction():
        activated = repo.collection.update_many(
            {
                "policy_document_id": doc_id,
                "document_version": document_version,
                "embedding_model": embedding_model,
            },
            {"$set": {"review_status": "approved", "is_active": True, "updated_at": now}},
            session=session,
        )
        deactivated = repo.collection.update_many(
            {
                "policy_document_id": doc_id,
                "document_version": {"$ne": document_version},
                "is_active": True,
            },
            {"$set": {"is_active": False, "updated_at": now}},
            session=session,
        )
    return ActivationResult(
        policy_document_id, document_version, activated.modified_count, deactivated.modified_count
    )


def deactivate_version(
    client: MongoClient[dict[str, Any]],
    repo: ChunkRepository,
    *,
    policy_document_id: int,
    document_version: str,
    review_status: str,
) -> int:
    """Apply a MySQL revocation/rejection. Does not touch other versions."""
    if review_status not in ("pending", "approved", "rejected"):
        raise ValidationFailedError("invalid review_status")
    try:
        result = repo.collection.update_many(
            {"policy_document_id": Int64(policy_document_id), "document_version": document_version},
            {"$set": {"is_active": False, "review_status": review_status, "updated_at": utc_now()}},
        )
    except OperationFailure as exc:
        raise ValidationFailedError(str(exc)) from exc
    return result.modified_count

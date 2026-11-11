"""Reconcile MongoDB chunk state with a MySQL ``policy_documents`` snapshot.

The snapshot is the source of truth. Planning is read-only; applying goes through
:func:`app.rag.publication.sync_policy_state` so the same checks and transaction rules hold.
Until a Spring listing API exists, the snapshot comes from a JSON export (AI-R2 body shape).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from bson.int64 import Int64
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from pymongo import MongoClient

from app.integrations.policy_gateway import MysqlReviewStatus, PolicyDocumentRecord
from app.rag.errors import ValidationFailedError
from app.rag.publication import sync_policy_state, target_state, verify_ready
from app.rag.repository import ChunkRepository
from app.rag.schemas import utc_now

PlanAction = Literal[
    "ok", "activate", "deactivate", "update", "not_ingested", "blocked", "deactivate_orphan"
]
CHANGES: frozenset[str] = frozenset({"activate", "deactivate", "update", "deactivate_orphan"})


class SnapshotDocument(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    policy_document_id: int = Field(ge=1)
    policy_version: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_status: MysqlReviewStatus
    searchable: bool
    chunk_count: int | None = Field(default=None, ge=0)

    def record(self) -> PolicyDocumentRecord:
        return PolicyDocumentRecord(
            self.policy_document_id,
            self.policy_version,
            self.content_hash,
            self.review_status,
            self.searchable,
        )


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    documents: list[SnapshotDocument]


def load_snapshot(path: Path) -> Snapshot:
    snapshot = Snapshot.model_validate(json.loads(path.read_text(encoding="utf-8")))
    keys = [(d.policy_document_id, d.policy_version) for d in snapshot.documents]
    ids = [d.policy_document_id for d in snapshot.documents]
    if len(set(ids)) != len(ids):
        # BE stores one version per row, so an id must not appear twice.
        raise ValidationFailedError(f"snapshot repeats a policyDocumentId: {keys}")
    return snapshot


@dataclass(frozen=True)
class MongoState:
    """Chunk state of one (policy_document_id, document_version)."""

    total: int = 0
    active: int = 0
    review_statuses: frozenset[str] = frozenset()
    # Active chunks under the same id but another document_version.
    stale_active: int = 0


@dataclass
class PlanItem:
    policy_document_id: int
    policy_version: str
    action: PlanAction
    reason: str = ""
    result: str = ""


@dataclass
class Plan:
    items: list[PlanItem] = field(default_factory=list)

    @property
    def changes(self) -> list[PlanItem]:
        return [i for i in self.items if i.action in CHANGES]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for item in self.items:
            out[item.action] = out.get(item.action, 0) + 1
        return out


def decide(
    record: PolicyDocumentRecord, state: MongoState, verify_error: str | None
) -> tuple[PlanAction, str]:
    """Pure decision for one snapshot row (unit-testable without MongoDB)."""
    review_status, active = target_state(record)
    if active:
        if state.total == 0:
            return "not_ingested", "approved and searchable but no chunks stored"
        if verify_error:
            return "blocked", verify_error
        if state.active == state.total and state.review_statuses == {review_status}:
            return ("deactivate", "stale versions active") if state.stale_active else ("ok", "")
        return "activate", ""
    if state.active or state.stale_active:
        return "deactivate", ""
    if state.total and state.review_statuses != {review_status}:
        return "update", f"review_status -> {review_status}"
    return "ok", ""


def mongo_state(
    repo: ChunkRepository, record: PolicyDocumentRecord, embedding_model: str
) -> MongoState:
    pid = Int64(record.policy_document_id)
    base: dict[str, Any] = {"policy_document_id": pid, "document_version": record.policy_version}
    _, active = target_state(record)
    # Activation is judged on the current model; deactivation covers every model.
    scoped = {**base, "embedding_model": embedding_model} if active else base
    total = repo.collection.count_documents(scoped)
    active_count = repo.collection.count_documents({**base, "is_active": True})
    statuses = frozenset(repo.collection.distinct("review_status", scoped))
    stale = repo.collection.count_documents(
        {
            "policy_document_id": pid,
            "document_version": {"$ne": record.policy_version},
            "is_active": True,
        }
    )
    return MongoState(total, active_count, statuses, stale)


def build_plan(
    repo: ChunkRepository,
    snapshot: Snapshot,
    *,
    embedding_model: str,
    complete: bool,
) -> Plan:
    plan = Plan()
    for doc in snapshot.documents:
        record = doc.record()
        state = mongo_state(repo, record, embedding_model)
        verify_error = None
        if target_state(record)[1] and state.total:
            try:
                verify_ready(repo, record, embedding_model, doc.chunk_count)
            except ValidationFailedError as exc:
                verify_error = str(exc)
        action, reason = decide(record, state, verify_error)
        plan.items.append(
            PlanItem(record.policy_document_id, record.policy_version, action, reason)
        )
    if complete:
        known = {d.policy_document_id for d in snapshot.documents}
        for pid in sorted(set(repo.collection.distinct("policy_document_id", {"is_active": True}))):
            if int(pid) not in known:
                plan.items.append(
                    PlanItem(int(pid), "*", "deactivate_orphan", "active in MongoDB, not in MySQL")
                )
    return plan


def apply_plan(
    client: MongoClient[dict[str, Any]],
    repo: ChunkRepository,
    snapshot: Snapshot,
    plan: Plan,
    *,
    embedding_model: str,
) -> Plan:
    by_id = {d.policy_document_id: d for d in snapshot.documents}
    for item in plan.changes:
        try:
            if item.action == "deactivate_orphan":
                modified = repo.collection.update_many(
                    {"policy_document_id": Int64(item.policy_document_id), "is_active": True},
                    {"$set": {"is_active": False, "updated_at": utc_now()}},
                ).modified_count
                item.result = f"deactivated {modified}"
                continue
            doc = by_id[item.policy_document_id]
            result = sync_policy_state(
                client,
                repo,
                doc.record(),
                embedding_model=embedding_model,
                expected_chunks=doc.chunk_count,
            )
            item.result = result.action
        except ValidationFailedError as exc:
            item.result = f"error: {exc}"
    return plan


def summarize(plan: Plan) -> dict[str, Any]:
    return {
        "counts": plan.counts(),
        "items": [i.__dict__ for i in plan.items if i.action != "ok"],
    }

"""MongoDB 청크 상태를 MySQL ``policy_documents`` 스냅샷과 대조해 맞춘다.

스냅샷이 기준 원본이다. 계획 수립은 읽기 전용이고, 적용은
:func:`app.rag.publication.sync_policy_state`를 거치므로 같은 검사와 트랜잭션 규칙이
유지된다. Spring 목록 API가 생기기 전까지 스냅샷은 JSON export(AI-R2 본문 형태)에서 가져온다.
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
    """스냅샷에 담긴 MySQL ``policy_documents`` 행 하나."""

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
    """상태 대조의 기준이 되는 MySQL ``policy_documents`` 스냅샷."""

    model_config = ConfigDict(extra="forbid")

    documents: list[SnapshotDocument]


def load_snapshot(path: Path) -> Snapshot:
    snapshot = Snapshot.model_validate(json.loads(path.read_text(encoding="utf-8")))
    keys = [(d.policy_document_id, d.policy_version) for d in snapshot.documents]
    ids = [d.policy_document_id for d in snapshot.documents]
    if len(set(ids)) != len(ids):
        # BE는 행마다 버전 하나를 저장하므로 같은 id가 두 번 나오면 안 된다.
        raise ValidationFailedError(f"snapshot repeats a policyDocumentId: {keys}")
    return snapshot


@dataclass(frozen=True)
class MongoState:
    """(policy_document_id, document_version) 하나의 청크 상태."""

    total: int = 0
    active: int = 0
    review_statuses: frozenset[str] = frozenset()
    # 같은 id에서 document_version이 다른 활성 청크.
    stale_active: int = 0


@dataclass
class PlanItem:
    """문서 하나에 대해 계획한 동작과 그 적용 결과."""

    policy_document_id: int
    policy_version: str
    action: PlanAction
    reason: str = ""
    result: str = ""


@dataclass
class Plan:
    """스냅샷 대조로 만든 PlanItem 목록."""

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
    """스냅샷 행 하나에 대한 순수 판단(MongoDB 없이 단위 테스트 가능)."""
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
    # 활성화는 현재 모델 기준으로 판단하고, 비활성화는 모든 모델을 대상으로 한다.
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

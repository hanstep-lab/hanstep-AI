"""MongoDB 청크 상태를 MySQL ``policy_documents`` 행 하나와 일치시킨다.

BE는 버전마다 별도 행으로 저장한다(``content_hash``는 유일). 따라서 활성화는 행마다 그 행의
MySQL 상태로 결정하며, 행을 넘나드는 "다른 버전 비활성화" 단계는 없다. MySQL과 MongoDB는
하나의 원자적 단위가 아니므로 호출자가 MySQL 상태를 기준으로 동기화를 다시 실행한다
(승인 이벤트 또는 주기적 상태 대조). 모든 동기화는 멱등이다.
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

# MySQL review_status -> (MongoDB review_status, 활성 가능 여부). BE 요청서의 제안 M1이다.
# BE가 다른 매핑으로 확정하면 이 표만 바꾼다.
REVIEW_STATE_MAP: dict[MysqlReviewStatus, tuple[ReviewStatus, bool]] = {
    "DRAFT": ("pending", False),
    "NEEDS_REVIEW": ("pending", False),
    "APPROVED": ("approved", True),
    "RETIRED": ("approved", False),
}

# "updated": review_status는 바뀌었지만 켜지거나 꺼진 것은 없다.
SyncAction = Literal["activated", "deactivated", "updated", "unchanged", "not_ingested"]


@dataclass(frozen=True)
class SyncResult:
    """MySQL 행 하나의 상태를 청크에 반영한 결과."""

    policy_document_id: int
    policy_version: str
    action: SyncAction
    review_status: ReviewStatus
    is_active: bool
    modified: int = 0
    deactivated: int = 0
    stale_deactivated: int = 0


def target_state(record: PolicyDocumentRecord) -> tuple[ReviewStatus, bool]:
    """MySQL이 APPROVED이고 *동시에* BE가 행을 searchable(예: 시행 중)로 표시할 때만 활성."""
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
    """행이 검색 가능해지기 전의 검사. 청크 수를 반환한다(0 = 미적재)."""
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
    """MySQL 행 하나의 상태를 그 행의 청크에 반영한다.

    - 활성화는 누락 없이 연속된 전체 청크 집합이 MySQL 해시(주어지면 ``chunk_count``도)와
      일치하는지 검증한 뒤, 현재 임베딩 모델의 청크만 건드린다.
    - 비활성화는 그 행의 모든 모델 청크에 적용한다(fail-safe 방향).
    - 같은 ``policy_document_id``에서 ``document_version``이 다른 청크는 MySQL이 이 행으로
      설명하는 대상이 아니므로 비활성화한다.
    - 활성화는 MongoDB 트랜잭션 안에서 실행하며, 트랜잭션을 지원하지 않으면 거부한다.
    """
    review_status, active = target_state(record)
    count = verify_ready(repo, record, embedding_model, expected_chunks) if active else 0
    pid = Int64(record.policy_document_id)

    target: dict[str, Any] = {"policy_document_id": pid, "document_version": record.policy_version}
    if active:
        target["embedding_model"] = embedding_model
    # 상태가 다른 문서만 건드려 반복 동기화가 실제로 no-op이 되게 한다.
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
            # review_status만 바뀐 경우를 비활성화로 보고하지 않도록 따로 센다.
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

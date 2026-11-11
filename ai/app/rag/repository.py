"""``visa_policy_chunks``의 MongoDB 저장소: validator, 고유 인덱스, 멱등 upsert."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from bson.int64 import Int64
from pymongo import ASCENDING, UpdateOne
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import BulkWriteError, CollectionInvalid

from app.rag.errors import ValidationFailedError, VersionConflictError
from app.rag.schemas import (
    UNIQUE_INDEX_KEYS,
    UNIQUE_INDEX_NAME,
    ChunkRecord,
    json_schema_validator,
)

PlanAction = Literal["insert_all", "resume", "noop"]


@dataclass(frozen=True)
class ExistingChunk:
    """이미 저장된 청크 중 쓰기 계획 비교에 필요한 필드."""

    chunk_index: int
    chunk_text: str
    source_sha256: str


@dataclass(frozen=True)
class UpsertPlan:
    """문서 버전 하나에 대한 쓰기 결정과 새로 넣을 청크 인덱스."""

    action: PlanAction
    missing_indexes: tuple[int, ...] = ()


def plan_version_write(
    existing: Sequence[ExistingChunk],
    *,
    source_sha256: str,
    expected_texts: Sequence[str] | None,
) -> UpsertPlan:
    """이미 저장된 내용을 보고 (문서, 버전, 모델) 하나를 어떻게 처리할지 정한다.

    청킹 전에는 ``expected_texts``가 ``None``이다(해시만 검사하며, 추출이나 임베딩 없이
    재수집을 건너뛸 때 쓴다).

    - 같은 버전인데 해시가 다름: 즉시 오류.
    - 해시가 같고 모든 청크가 같은 텍스트로 있음: no-op(승인 상태는 건드리지 않는다).
    - 해시가 같고 앞쪽 일부(strict prefix)만 같은 텍스트로 있음: 빠진 청크를 이어 넣는다.
    - 해시가 같은데 텍스트가 다르거나 청크가 더 많음: 기존 버전의 청킹/파서 설정이 바뀐
      것이다. 명시적인 재처리 정책이 필요하므로 오류로 본다.
    """
    for chunk in existing:
        if chunk.source_sha256 != source_sha256:
            raise VersionConflictError(
                "document_version already stored with a different source_sha256; "
                "a changed source requires a new document_version"
            )
    if expected_texts is None:
        return UpsertPlan("noop" if existing else "insert_all")

    stored = {chunk.chunk_index: chunk.chunk_text for chunk in existing}
    if not stored:
        return UpsertPlan("insert_all", tuple(range(len(expected_texts))))
    extra = sorted(index for index in stored if index >= len(expected_texts))
    if extra:
        raise VersionConflictError(
            f"stored chunks {extra} do not exist in the new chunking; reprocessing policy required"
        )
    for index, text in stored.items():
        if expected_texts[index] != text:
            raise VersionConflictError(
                f"chunk {index} text differs from stored text; parser/chunker settings changed "
                "for an existing version, reprocessing policy required"
            )
    missing = tuple(i for i in range(len(expected_texts)) if i not in stored)
    return UpsertPlan("resume" if missing else "noop", missing)


@dataclass
class WriteResult:
    """청크 일괄 쓰기 결과(새로 넣은 수, 이미 있던 수, 오류)."""

    inserted: int = 0
    already_present: int = 0
    errors: list[str] = field(default_factory=list)


class ChunkRepository:
    """청크 컬렉션의 설정·조회·쓰기를 맡는 MongoDB 저장소."""

    def __init__(self, database: Database[dict[str, Any]], collection_name: str) -> None:
        self.database = database
        self.collection_name = collection_name

    @property
    def collection(self) -> Collection[dict[str, Any]]:
        return self.database[self.collection_name]

    # --- 설정 --------------------------------------------------------------------------

    def validator_status(self) -> tuple[str, dict[str, Any] | None]:
        """("missing" | "match" | "drift", 현재 validator)를 반환한다."""
        infos = list(self.database.list_collections(filter={"name": self.collection_name}))
        if not infos:
            return "missing", None
        current = infos[0].get("options", {}).get("validator")
        return ("match" if current == json_schema_validator() else "drift"), current

    def ensure_collection(self) -> str:
        """컬렉션이 없으면 validator와 함께 만든다. 기존 validator는 절대 덮어쓰지 않는다."""
        status, current = self.validator_status()
        if status == "missing":
            try:
                self.database.create_collection(
                    self.collection_name,
                    validator=json_schema_validator(),
                    validationLevel="strict",
                    validationAction="error",
                )
            except CollectionInvalid:
                return self.validator_status()[0]
            return "created"
        if status == "drift" and current is None:
            return "drift:no-validator"
        return status

    def unique_index_status(self) -> str:
        for index in self.collection.list_indexes():
            if list(index["key"].items()) == UNIQUE_INDEX_KEYS:
                return "match" if index.get("unique") else "drift:not-unique"
        return "missing"

    def find_duplicates(self) -> list[dict[str, Any]]:
        pipeline: list[dict[str, Any]] = [
            {"$group": {"_id": {k: f"${k}" for k, _ in UNIQUE_INDEX_KEYS}, "n": {"$sum": 1}}},
            {"$match": {"n": {"$gt": 1}}},
            {"$limit": 100},
        ]
        return list(self.collection.aggregate(pipeline))

    def ensure_unique_index(self) -> str:
        status = self.unique_index_status()
        if status != "missing":
            return status
        duplicates = self.find_duplicates()
        if duplicates:
            # 보고만 하고 절대 삭제하지 않는다.
            raise ValidationFailedError(f"duplicate chunk keys exist: {duplicates[:5]}")
        self.collection.create_index(
            [(k, ASCENDING) for k, _ in UNIQUE_INDEX_KEYS], unique=True, name=UNIQUE_INDEX_NAME
        )
        return "created"

    # --- 조회 --------------------------------------------------------------------------

    @staticmethod
    def _version_filter(
        policy_document_id: int, document_version: str, embedding_model: str
    ) -> dict[str, Any]:
        return {
            "policy_document_id": Int64(policy_document_id),
            "document_version": document_version,
            "embedding_model": embedding_model,
        }

    def existing_chunks(
        self, policy_document_id: int, document_version: str, embedding_model: str
    ) -> list[ExistingChunk]:
        cursor = self.collection.find(
            self._version_filter(policy_document_id, document_version, embedding_model),
            {"chunk_index": 1, "chunk_text": 1, "source_sha256": 1},
        ).sort("chunk_index", ASCENDING)
        return [
            ExistingChunk(doc["chunk_index"], doc["chunk_text"], doc["source_sha256"])
            for doc in cursor
        ]

    def load_version(
        self, policy_document_id: int, document_version: str, embedding_model: str
    ) -> list[ChunkRecord]:
        cursor = self.collection.find(
            self._version_filter(policy_document_id, document_version, embedding_model)
        ).sort("chunk_index", ASCENDING)
        return [ChunkRecord.from_bson(doc) for doc in cursor]

    # --- 쓰기 --------------------------------------------------------------------------

    def insert_chunks(self, records: Sequence[ChunkRecord]) -> WriteResult:
        """고유 인덱스를 키로 하는 삽입 전용 upsert.

        ``$setOnInsert``를 쓰므로 기존 레코드(review_status, is_active, created_at 포함)는
        절대 수정되지 않는다. Ordered=False라서 실패 하나가 배치 전체를 멈추지 않으며,
        다시 실행하면 아직 빠진 것을 채운다.
        """
        result = WriteResult()
        if not records:
            return result
        for record in records:
            if record.review_status != "pending" or record.is_active:
                raise ValidationFailedError("new chunks must be pending and inactive")
        operations = []
        for record in records:
            doc = record.to_bson()
            key = {k: doc[k] for k, _ in UNIQUE_INDEX_KEYS}
            operations.append(UpdateOne(key, {"$setOnInsert": doc}, upsert=True))
        try:
            bulk = self.collection.bulk_write(operations, ordered=False)
            result.inserted = bulk.upserted_count
            result.already_present = len(records) - bulk.upserted_count
        except BulkWriteError as exc:
            details = exc.details
            result.inserted = details.get("nUpserted", 0)
            for error in details.get("writeErrors", []):
                # 11000 = 같은 키의 동시 삽입. 그 내용은 재실행 때 검증한다.
                if error.get("code") == 11000:
                    result.already_present += 1
                else:
                    result.errors.append(f"index {error.get('index')}: {error.get('errmsg')}")
        return result

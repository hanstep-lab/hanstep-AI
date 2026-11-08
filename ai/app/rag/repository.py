"""MongoDB persistence for ``visa_policy_chunks``: validator, unique index, idempotent upsert."""

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
    chunk_index: int
    chunk_text: str
    source_sha256: str


@dataclass(frozen=True)
class UpsertPlan:
    action: PlanAction
    missing_indexes: tuple[int, ...] = ()


def plan_version_write(
    existing: Sequence[ExistingChunk],
    *,
    source_sha256: str,
    expected_texts: Sequence[str] | None,
) -> UpsertPlan:
    """Decide what to do with one (document, version, model) given what is already stored.

    ``expected_texts`` is ``None`` before chunking (hash-only check, used to skip re-collection
    without extracting or embedding).

    - Different hash for the same version: hard error.
    - Same hash, all chunks present with identical text: no-op (approval state untouched).
    - Same hash, a strict prefix present with identical text: resume missing chunks.
    - Same hash, different text or extra chunks: chunking/parser settings changed for an
      existing version; requires an explicit reprocessing policy, so it is an error.
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
    inserted: int = 0
    already_present: int = 0
    errors: list[str] = field(default_factory=list)


class ChunkRepository:
    def __init__(self, database: Database[dict[str, Any]], collection_name: str) -> None:
        self.database = database
        self.collection_name = collection_name

    @property
    def collection(self) -> Collection[dict[str, Any]]:
        return self.database[self.collection_name]

    # --- setup -------------------------------------------------------------------------

    def validator_status(self) -> tuple[str, dict[str, Any] | None]:
        """Return ("missing" | "match" | "drift", current validator)."""
        infos = list(self.database.list_collections(filter={"name": self.collection_name}))
        if not infos:
            return "missing", None
        current = infos[0].get("options", {}).get("validator")
        return ("match" if current == json_schema_validator() else "drift"), current

    def ensure_collection(self) -> str:
        """Create the collection with the validator if missing. Never overwrites a validator."""
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
            # Report, never delete.
            raise ValidationFailedError(f"duplicate chunk keys exist: {duplicates[:5]}")
        self.collection.create_index(
            [(k, ASCENDING) for k, _ in UNIQUE_INDEX_KEYS], unique=True, name=UNIQUE_INDEX_NAME
        )
        return "created"

    # --- reads -------------------------------------------------------------------------

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

    # --- writes ------------------------------------------------------------------------

    def insert_chunks(self, records: Sequence[ChunkRecord]) -> WriteResult:
        """Insert-only upsert keyed by the unique index.

        ``$setOnInsert`` means an existing record (including its review_status, is_active and
        created_at) is never modified. Ordered=False so one failure does not stop the batch;
        a rerun picks up whatever is still missing.
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
                # 11000 = concurrent insert of the same key; the rerun verifies its content.
                if error.get("code") == 11000:
                    result.already_present += 1
                else:
                    result.errors.append(f"index {error.get('index')}: {error.get('errmsg')}")
        return result

"""Confirmed MongoDB contract for ``shinhan_rag.visa_policy_chunks``.

Field names and BSON types follow specs/rag/decisions.md section 1. Do not rename fields or
add new persisted fields here without a contract change.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import Any, Literal

from bson import ObjectId
from bson.int64 import Int64
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

COLLECTION_NAME = "visa_policy_chunks"
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-m3"
EMBEDDING_DIMENSIONS = 1024
INT32_MIN, INT32_MAX = -(2**31), 2**31 - 1
INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1

ReviewStatus = Literal["pending", "approved", "rejected"]
REVIEW_STATUSES: tuple[str, ...] = ("pending", "approved", "rejected")

UNIQUE_INDEX_KEYS: list[tuple[str, int]] = [
    ("policy_document_id", 1),
    ("document_version", 1),
    ("chunk_index", 1),
    ("embedding_model", 1),
]
UNIQUE_INDEX_NAME = "uniq_document_version_chunk_model"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def utc_now() -> datetime:
    # BSON Date has millisecond precision; truncate so round-trips compare equal.
    now = datetime.now(UTC)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def validate_embedding(vector: Any, dimensions: int = EMBEDDING_DIMENSIONS) -> list[float]:
    """Return the vector as floats or raise ``ValueError``.

    Rejects bools, NaN/Infinity, wrong lengths, empty and zero vectors.
    """
    if not isinstance(vector, list | tuple):
        raise ValueError("embedding must be a list of numbers")
    if len(vector) != dimensions:
        raise ValueError(f"embedding must have {dimensions} dimensions, got {len(vector)}")
    values: list[float] = []
    for item in vector:
        if isinstance(item, bool) or not isinstance(item, int | float):
            raise ValueError("embedding values must be numbers")
        number = float(item)
        if not math.isfinite(number):
            raise ValueError("embedding values must be finite")
        values.append(number)
    if not any(values):
        raise ValueError("embedding must not be a zero vector")
    return values


def _require_text(value: str, name: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value


class ChunkRecord(BaseModel):
    """One chunk document as stored in MongoDB."""

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True, frozen=True)

    id: ObjectId | None = Field(default=None, alias="_id")
    policy_document_id: int = Field(ge=1, le=INT64_MAX)
    document_version: str
    chunk_index: int = Field(ge=0, le=INT32_MAX)
    chunk_text: str
    embedding: list[float]
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    source_sha256: str
    title: str
    source_url: str
    source_org: str
    visa_types: list[str]
    language: str = "ko"
    review_status: ReviewStatus = "pending"
    is_active: bool = False
    created_at: datetime
    updated_at: datetime
    page_number: int | None = Field(default=None, ge=1, le=INT32_MAX)
    section_title: str | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None

    @field_validator("policy_document_id", "chunk_index", "page_number", mode="before")
    @classmethod
    def _no_bool_ints(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("integer field must not be a bool")
        if isinstance(value, Int64):
            return int(value)
        return value

    @field_validator("embedding", mode="before")
    @classmethod
    def _check_embedding(cls, value: Any) -> list[float]:
        return validate_embedding(value)

    @field_validator("source_sha256")
    @classmethod
    def _check_sha(cls, value: str) -> str:
        if not _SHA256.match(value):
            raise ValueError("source_sha256 must be 64 lowercase hex characters")
        return value

    @field_validator(
        "document_version", "chunk_text", "title", "source_url", "source_org", "embedding_model"
    )
    @classmethod
    def _non_empty(cls, value: str, info: Any) -> str:
        return _require_text(value, info.field_name)

    @field_validator("visa_types")
    @classmethod
    def _check_visa_types(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("visa_types must not be empty")
        for item in value:
            _require_text(item, "visa_types item")
        return value

    @field_validator("section_title")
    @classmethod
    def _optional_text(cls, value: str | None) -> str | None:
        if value is not None:
            _require_text(value, "section_title")
        return value

    @field_validator("created_at", "updated_at", "effective_from", "effective_to")
    @classmethod
    def _aware_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            # PyMongo returns naive UTC datetimes unless tz_aware=True.
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _check_consistency(self) -> ChunkRecord:
        if self.is_active and self.review_status != "approved":
            raise ValueError("is_active=true requires review_status=approved")
        if self.effective_from and self.effective_to and self.effective_from > self.effective_to:
            raise ValueError("effective_from must not be after effective_to")
        return self

    def to_bson(self) -> dict[str, Any]:
        """Return a document with explicit BSON types (Int64 id, Int32 ints, Date, ObjectId)."""
        doc: dict[str, Any] = {
            "policy_document_id": Int64(self.policy_document_id),
            "document_version": self.document_version,
            "chunk_index": int(self.chunk_index),
            "chunk_text": self.chunk_text,
            "embedding": list(self.embedding),
            "embedding_model": self.embedding_model,
            "source_sha256": self.source_sha256,
            "title": self.title,
            "source_url": self.source_url,
            "source_org": self.source_org,
            "visa_types": list(self.visa_types),
            "language": self.language,
            "review_status": self.review_status,
            "is_active": self.is_active,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.id is not None:
            doc["_id"] = self.id
        # Optional fields are omitted, never stored as null.
        for name in ("page_number", "section_title", "effective_from", "effective_to"):
            value = getattr(self, name)
            if value is not None:
                doc[name] = value
        return doc

    @classmethod
    def from_bson(cls, doc: dict[str, Any]) -> ChunkRecord:
        return cls.model_validate(doc)


def json_schema_validator(dimensions: int = EMBEDDING_DIMENSIONS) -> dict[str, Any]:
    """``$jsonSchema`` validator mirroring :class:`ChunkRecord`.

    Finite-value checks for embeddings stay in the application (``validate_embedding``).
    """
    required = [
        "_id",
        "policy_document_id",
        "document_version",
        "chunk_index",
        "chunk_text",
        "embedding",
        "embedding_model",
        "source_sha256",
        "title",
        "source_url",
        "source_org",
        "visa_types",
        "language",
        "review_status",
        "is_active",
        "created_at",
        "updated_at",
    ]
    non_empty = {"bsonType": "string", "minLength": 1}
    return {
        "$jsonSchema": {
            "bsonType": "object",
            "required": required,
            "properties": {
                "_id": {"bsonType": "objectId"},
                "policy_document_id": {"bsonType": "long", "minimum": 1},
                "document_version": non_empty,
                "chunk_index": {"bsonType": "int", "minimum": 0},
                "chunk_text": non_empty,
                "embedding": {
                    "bsonType": "array",
                    "minItems": dimensions,
                    "maxItems": dimensions,
                    "items": {"bsonType": ["double", "int", "long", "decimal"]},
                },
                "embedding_model": non_empty,
                "source_sha256": {"bsonType": "string", "pattern": "^[0-9a-f]{64}$"},
                "title": non_empty,
                "source_url": non_empty,
                "source_org": non_empty,
                "visa_types": {"bsonType": "array", "minItems": 1, "items": non_empty},
                "language": non_empty,
                "review_status": {"enum": list(REVIEW_STATUSES)},
                "is_active": {"bsonType": "bool"},
                "created_at": {"bsonType": "date"},
                "updated_at": {"bsonType": "date"},
                "page_number": {"bsonType": "int", "minimum": 1},
                "section_title": non_empty,
                "effective_from": {"bsonType": "date"},
                "effective_to": {"bsonType": "date"},
            },
            # is_active=true requires review_status=approved (the reverse is not enforced).
            "anyOf": [
                {"properties": {"is_active": {"enum": [False]}}},
                {"properties": {"review_status": {"enum": ["approved"]}}},
            ],
        }
    }

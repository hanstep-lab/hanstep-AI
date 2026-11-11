"""``shinhan_rag.visa_policy_chunks``의 확정된 MongoDB 계약.

필드 이름과 BSON 타입은 specs/rag/decisions.md 1절을 따른다. 계약 변경 없이 여기서
필드 이름을 바꾸거나 새 저장 필드를 추가하지 않는다.
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
# 소문자로 바꾼 MySQL policy_documents.source_type
# (LAW/ADMIN_RULE/NOTICE/GUIDE/SCORE_TABLE).
SourceType = Literal["law", "admin_rule", "notice", "guide", "score_table"]
SOURCE_TYPES: tuple[str, ...] = ("law", "admin_rule", "notice", "guide", "score_table")

UNIQUE_INDEX_KEYS: list[tuple[str, int]] = [
    ("policy_document_id", 1),
    ("document_version", 1),
    ("chunk_index", 1),
    ("embedding_model", 1),
]
# RAG 설계 명세 v1.0의 이름. ensure_unique_index는 기존 인덱스를 키로 대조한다.
UNIQUE_INDEX_NAME = "ux_policy_version_chunk_model"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def utc_now() -> datetime:
    # BSON Date는 밀리초 정밀도다. 왕복 후에도 같게 비교되도록 잘라낸다.
    now = datetime.now(UTC)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def validate_embedding(vector: Any, dimensions: int = EMBEDDING_DIMENSIONS) -> list[float]:
    """벡터를 float 목록으로 반환하거나 ``ValueError``를 발생시킨다.

    bool, NaN/Infinity, 잘못된 길이, 빈 벡터와 영벡터는 거부한다.
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
    """MongoDB에 저장되는 청크 문서 하나."""

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
    source_type: SourceType
    language: str = "ko"
    review_status: ReviewStatus = "pending"
    is_active: bool = False
    created_at: datetime
    updated_at: datetime
    page_number: int | None = Field(default=None, ge=1, le=INT32_MAX)
    section_title: str | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    # 발행일(MySQL published_on). 공식 출처에서 확인된 경우에만 넣는다.
    issued_at: datetime | None = None

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

    @field_validator("created_at", "updated_at", "effective_from", "effective_to", "issued_at")
    @classmethod
    def _aware_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            # PyMongo는 tz_aware=True가 아니면 naive UTC datetime을 반환한다.
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
        """명시적인 BSON 타입(Int64 id, Int32 정수, Date, ObjectId)으로 된 문서를 반환한다."""
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
            "source_type": self.source_type,
            "visa_types": list(self.visa_types),
            "language": self.language,
            "review_status": self.review_status,
            "is_active": self.is_active,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.id is not None:
            doc["_id"] = self.id
        # 선택 필드는 null로 저장하지 않고 생략한다.
        for name in ("page_number", "section_title", "effective_from", "effective_to", "issued_at"):
            value = getattr(self, name)
            if value is not None:
                doc[name] = value
        return doc

    @classmethod
    def from_bson(cls, doc: dict[str, Any]) -> ChunkRecord:
        return cls.model_validate(doc)


def json_schema_validator(dimensions: int = EMBEDDING_DIMENSIONS) -> dict[str, Any]:
    """:class:`ChunkRecord`를 그대로 반영한 ``$jsonSchema`` validator.

    임베딩의 유한값 검사는 애플리케이션(``validate_embedding``)에 남겨 둔다.
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
        "source_type",
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
                "source_type": {"enum": list(SOURCE_TYPES)},
                "review_status": {"enum": list(REVIEW_STATUSES)},
                "is_active": {"bsonType": "bool"},
                "created_at": {"bsonType": "date"},
                "updated_at": {"bsonType": "date"},
                "page_number": {"bsonType": "int", "minimum": 1},
                "section_title": non_empty,
                "effective_from": {"bsonType": "date"},
                "effective_to": {"bsonType": "date"},
                "issued_at": {"bsonType": "date"},
            },
            # is_active=true면 review_status=approved여야 한다(반대 방향은 강제하지 않는다).
            "anyOf": [
                {"properties": {"is_active": {"enum": [False]}}},
                {"properties": {"review_status": {"enum": ["approved"]}}},
            ],
        }
    }

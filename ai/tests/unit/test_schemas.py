from __future__ import annotations

import math
from datetime import UTC, datetime

import bson
import pytest
from bson import ObjectId
from bson.int64 import Int64
from pydantic import ValidationError

from app.rag.schemas import ChunkRecord, json_schema_validator, validate_embedding
from tests.conftest import record_kwargs

pytestmark = pytest.mark.unit


def test_bson_round_trip_keeps_explicit_types() -> None:
    record = ChunkRecord(**record_kwargs(page_number=3, section_title="1 선발인원"))
    doc = record.to_bson()
    doc["_id"] = ObjectId()
    raw = bson.encode(doc)
    decoded = bson.decode(raw, codec_options=bson.CodecOptions(tz_aware=True))

    assert isinstance(doc["policy_document_id"], Int64)
    # Type bytes in the encoded document: 0x12 = int64, 0x10 = int32, 0x09 = UTC datetime.
    assert b"\x12policy_document_id\x00" in raw
    assert b"\x10chunk_index\x00" in raw
    assert b"\x10page_number\x00" in raw
    assert b"\x09created_at\x00" in raw
    assert isinstance(decoded["_id"], ObjectId)
    assert decoded["created_at"] == record.created_at
    assert ChunkRecord.from_bson(decoded).to_bson()["chunk_text"] == record.chunk_text


def test_optional_fields_are_omitted_not_null() -> None:
    doc = ChunkRecord(**record_kwargs()).to_bson()
    for name in (
        "page_number",
        "section_title",
        "effective_from",
        "effective_to",
        "issued_at",
        "_id",
    ):
        assert name not in doc


@pytest.mark.parametrize(
    "vector",
    [
        [0.1] * 1023,
        [0.1] * 1025,
        [],
        [0.0] * 1024,
        [0.1] * 1023 + [math.nan],
        [0.1] * 1023 + [math.inf],
        [0.1] * 1023 + [True],
        "not a list",
    ],
)
def test_invalid_embeddings_rejected(vector: object) -> None:
    with pytest.raises(ValueError):
        validate_embedding(vector)
    with pytest.raises(ValidationError):
        ChunkRecord(**record_kwargs(embedding=vector))


@pytest.mark.parametrize(
    "overrides",
    [
        {"chunk_text": "   "},
        {"title": ""},
        {"source_sha256": "A" * 64},
        {"source_sha256": "abc"},
        {"visa_types": []},
        {"review_status": "APPROVED"},
        {"source_type": "NOTICE"},  # stored lower-case
        {"source_type": "faq"},
        {"policy_document_id": 0},
        {"policy_document_id": True},
        {"chunk_index": 2**31},
        {"chunk_index": -1},
        {"page_number": 0},
        {"is_active": True, "review_status": "pending"},
        {
            "effective_from": datetime(2026, 7, 1, tzinfo=UTC),
            "effective_to": datetime(2026, 1, 1, tzinfo=UTC),
        },
    ],
)
def test_contract_violations_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ChunkRecord(**record_kwargs(**overrides))


def test_approved_inactive_is_allowed() -> None:
    record = ChunkRecord(**record_kwargs(review_status="approved", is_active=False))
    assert record.is_active is False


def test_naive_datetimes_from_pymongo_are_treated_as_utc() -> None:
    naive = datetime(2026, 1, 1, 0, 0)
    record = ChunkRecord(**record_kwargs(created_at=naive, updated_at=naive))
    assert record.created_at.tzinfo is UTC


def test_validator_mirrors_contract() -> None:
    schema = json_schema_validator()["$jsonSchema"]
    props = schema["properties"]
    assert props["policy_document_id"]["bsonType"] == "long"
    assert props["chunk_index"]["bsonType"] == "int"
    assert props["embedding"]["minItems"] == props["embedding"]["maxItems"] == 1024
    assert props["review_status"]["enum"] == ["pending", "approved", "rejected"]
    assert props["source_type"]["enum"] == ["law", "admin_rule", "notice", "guide", "score_table"]
    assert props["issued_at"]["bsonType"] == "date"
    assert "source_type" in schema["required"]
    assert "issued_at" not in schema["required"]
    assert "page_number" not in schema["required"]
    assert {"_id", "created_at", "updated_at", "embedding"} <= set(schema["required"])

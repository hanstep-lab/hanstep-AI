from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from app.rag.chunking import approx_token_count
from app.rag.repository import ExistingChunk, WriteResult
from app.rag.schemas import ChunkRecord

AI_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = AI_ROOT.parent / "data" / "rag_documents"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fake_vector(text: str, dims: int = 1024) -> list[float]:
    digest = hashlib.sha256(text.encode()).digest()
    values = [((digest[i % 32] + i) % 97) / 97.0 + 0.01 for i in range(dims)]
    norm = sum(v * v for v in values) ** 0.5
    return [v / norm for v in values]


class FakeEmbedder:
    model_name = "BAAI/bge-m3"

    def __init__(self) -> None:
        self.document_calls = 0
        self.embedded_texts = 0

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.document_calls += 1
        self.embedded_texts += len(texts)
        return [fake_vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return fake_vector(text)

    def count_tokens(self, text: str) -> int:
        return approx_token_count(text)


class FakeRepository:
    """Dict-backed stand-in mirroring ChunkRepository's insert-only semantics."""

    def __init__(self) -> None:
        self.docs: dict[tuple[int, str, int, str], ChunkRecord] = {}
        self.fail_after: int | None = None
        self.writes = 0

    def unique_index_status(self) -> str:
        return "match"

    def existing_chunks(self, pid: int, version: str, model: str) -> list[ExistingChunk]:
        return [
            ExistingChunk(r.chunk_index, r.chunk_text, r.source_sha256)
            for r in self.load_version(pid, version, model)
        ]

    def load_version(self, pid: int, version: str, model: str) -> list[ChunkRecord]:
        return sorted(
            (r for k, r in self.docs.items() if k[0] == pid and k[1] == version and k[3] == model),
            key=lambda r: r.chunk_index,
        )

    def insert_chunks(self, records: Sequence[ChunkRecord]) -> WriteResult:
        result = WriteResult()
        for i, record in enumerate(records):
            if self.fail_after is not None and i >= self.fail_after:
                result.errors.append(f"index {i}: simulated failure")
                continue
            key = (
                record.policy_document_id,
                record.document_version,
                record.chunk_index,
                record.embedding_model,
            )
            self.writes += 1
            if key in self.docs:
                result.already_present += 1
            else:
                self.docs[key] = record
                result.inserted += 1
        return result

    def set_state(self, pid: int, version: str, review_status: str, active: bool) -> None:
        for key, record in list(self.docs.items()):
            if key[0] == pid and key[1] == version:
                self.docs[key] = record.model_copy(
                    update={"review_status": review_status, "is_active": active}
                )


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def fake_repository() -> FakeRepository:
    return FakeRepository()


def record_kwargs(**overrides: Any) -> dict[str, Any]:
    from datetime import UTC, datetime

    now = datetime(2026, 11, 8, 3, 0, tzinfo=UTC)
    base: dict[str, Any] = {
        "policy_document_id": 7,
        "document_version": "2026-06-01",
        "chunk_index": 0,
        "chunk_text": "숙련기능인력(E-7-4) 선발 계획",
        "embedding": fake_vector("x"),
        "source_sha256": "a" * 64,
        "title": "2026년 숙련기능인력 선발 계획",
        "source_url": "https://www.hikorea.go.kr/board/1",
        "source_org": "법무부",
        "visa_types": ["E-7-4"],
        "created_at": now,
        "updated_at": now,
    }
    base.update(overrides)
    return base

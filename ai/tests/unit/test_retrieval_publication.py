from __future__ import annotations

from typing import Any

import pytest
from bson import ObjectId

from app.integrations.policy_gateway import (
    InMemoryPolicyGateway,
    PolicyDocumentRecord,
    UnconfiguredPolicyGateway,
)
from app.rag.errors import ValidationFailedError
from app.rag.publication import REVIEW_STATE_MAP, sync_policy_state, target_state
from app.rag.retrieval import (
    SearchService,
    SearchUnavailableError,
    build_pipeline,
    filter_current,
    validate_visa_types,
)
from tests.conftest import FakeEmbedder, FakeRepository, fake_vector

pytestmark = pytest.mark.unit


def _hit(
    pid: int, version: str, *, active: bool = True, status: str = "approved"
) -> dict[str, Any]:
    return {
        "_id": ObjectId(),
        "policy_document_id": pid,
        "document_version": version,
        "chunk_index": 0,
        "chunk_text": "본문",
        "title": "t",
        "source_url": "https://www.moj.go.kr/x",
        "source_org": "법무부",
        "visa_types": ["E-7-4"],
        "source_type": "notice",
        "is_active": active,
        "review_status": status,
        "score": 0.9,
    }


def test_pipeline_always_enforces_approval_filter() -> None:
    pipeline = build_pipeline(
        index_name="rag_vector_index",
        query_vector=fake_vector("q"),
        top_k=5,
        num_candidates=100,
        visa_types=["E-7-4"],
        embedding_model="BAAI/bge-m3",
    )
    stage = pipeline[0]["$vectorSearch"]
    assert stage["filter"] == {
        "is_active": True,
        "review_status": "approved",
        "visa_types": {"$in": ["E-7-4"]},
    }
    # 다른 임베딩 모델의 청크는 projection과 점수 계산 전에 걸러진다.
    assert pipeline[1] == {"$match": {"embedding_model": "BAAI/bge-m3"}}
    assert pipeline[2] == {"$project": {"embedding": 0}}
    assert pipeline[3] == {"$set": {"score": {"$meta": "vectorSearchScore"}}}


@pytest.mark.parametrize("bad", [["E-7-4", {"$ne": 1}], ["e74"], ["E-7-4; drop"], ["X"] * 11])
def test_visa_types_are_validated(bad: list[Any]) -> None:
    with pytest.raises(ValidationFailedError):
        validate_visa_types(bad)


def test_invalid_top_k_rejected() -> None:
    with pytest.raises(ValidationFailedError):
        build_pipeline(
            index_name="i",
            query_vector=[1.0],
            top_k=0,
            num_candidates=10,
            visa_types=[],
            embedding_model="BAAI/bge-m3",
        )


def test_filter_current_drops_stale_and_unapproved_hits() -> None:
    gw = InMemoryPolicyGateway()
    gw.add(PolicyDocumentRecord(1, "v2", "a" * 64, "APPROVED", searchable=True))
    gw.add(PolicyDocumentRecord(1, "v1", "a" * 64, "RETIRED"))
    hits = [_hit(1, "v2"), _hit(1, "v1"), _hit(1, "v2", active=False), _hit(2, "v1")]
    kept = filter_current(hits, gw)
    assert kept == [hits[0]]


@pytest.mark.parametrize("gateway", [UnconfiguredPolicyGateway(), InMemoryPolicyGateway(fail=True)])
def test_filter_current_fails_closed(gateway: Any) -> None:
    with pytest.raises(SearchUnavailableError):
        filter_current([_hit(1, "v1")], gateway)


class FakeCollection:
    """aggregate에 받은 파이프라인을 기록하고 정해 둔 문서를 돌려주는 가짜 컬렉션."""

    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.docs = docs
        self.pipelines: list[list[dict[str, Any]]] = []

    def aggregate(self, pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.pipelines.append(pipeline)
        return self.docs


def test_english_query_uses_same_embedder_and_server_filter() -> None:
    gw = InMemoryPolicyGateway()
    gw.add(PolicyDocumentRecord(1, "v1", "a" * 64, "APPROVED", searchable=True))
    # 인덱스 지연을 흉내 낸다: 검색 인덱스가 pending 청크를 아직 돌려준다.
    collection = FakeCollection([_hit(1, "v1"), _hit(1, "v1", active=False, status="pending")])
    service = SearchService(
        collection,  # type: ignore[arg-type]
        FakeEmbedder(),
        gw,
        index_name="rag_vector_index",
        num_candidates=100,
    )
    hits = service.search("Which documents are required for the E-7-4 recommendation?", 5, None)
    assert len(hits) == 1
    assert collection.pipelines[0][0]["$vectorSearch"]["filter"]["review_status"] == "approved"
    assert collection.pipelines[0][1] == {"$match": {"embedding_model": "BAAI/bge-m3"}}
    # numCandidates = max(topK * 20, 설정된 하한)
    assert collection.pipelines[0][0]["$vectorSearch"]["numCandidates"] == 100
    service.search("q", 8, None)
    assert collection.pipelines[1][0]["$vectorSearch"]["numCandidates"] == 160


class UntouchableClient:
    """어떤 속성에 접근해도 AssertionError를 내는 가짜 MongoDB 클라이언트."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError("activation must not touch MongoDB before checks pass")


@pytest.mark.parametrize(
    ("status", "searchable", "expected"),
    [
        ("DRAFT", False, ("pending", False)),
        ("DRAFT", True, ("pending", False)),  # searchable만으로는 활성화되지 않는다
        ("NEEDS_REVIEW", True, ("pending", False)),
        ("APPROVED", True, ("approved", True)),
        ("APPROVED", False, ("approved", False)),  # 승인됐지만 아직 시행 전
        ("RETIRED", True, ("approved", False)),
    ],
)
def test_target_state_mapping(status: Any, searchable: bool, expected: tuple[str, bool]) -> None:
    assert target_state(PolicyDocumentRecord(1, "v1", "a" * 64, status, searchable)) == expected


def test_mapping_covers_every_mysql_status() -> None:
    assert set(REVIEW_STATE_MAP) == {"DRAFT", "APPROVED", "NEEDS_REVIEW", "RETIRED"}


def _stored(repo: FakeRepository, n: int, sha: str = "a" * 64, start: int = 0) -> None:
    from app.rag.schemas import ChunkRecord
    from tests.conftest import record_kwargs

    for i in range(start, start + n):
        record = ChunkRecord(
            **record_kwargs(
                policy_document_id=1, document_version="v2", chunk_index=i, source_sha256=sha
            )
        )
        repo.docs[(1, "v2", i, "BAAI/bge-m3")] = record


@pytest.mark.parametrize(
    ("setup", "expected_chunks", "record_hash"),
    [
        (lambda r: _stored(r, 2, start=1), None, "a" * 64),  # 번호 빈틈: 청크 0이 없다
        (lambda r: _stored(r, 2, sha="b" * 64), None, "a" * 64),  # MySQL과 해시가 다르다
        (lambda r: _stored(r, 2), 3, "a" * 64),  # MySQL chunk_count와 불일치
    ],
)
def test_activation_preconditions_block_before_any_write(
    setup: Any, expected_chunks: int | None, record_hash: str
) -> None:
    repo = FakeRepository()
    setup(repo)
    with pytest.raises(ValidationFailedError):
        sync_policy_state(
            UntouchableClient(),  # type: ignore[arg-type]
            repo,  # type: ignore[arg-type]
            PolicyDocumentRecord(1, "v2", record_hash, "APPROVED", searchable=True),
            embedding_model="BAAI/bge-m3",
            expected_chunks=expected_chunks,
        )

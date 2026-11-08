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
from app.rag.publication import activate_version
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
    )
    stage = pipeline[0]["$vectorSearch"]
    assert stage["filter"] == {
        "is_active": True,
        "review_status": "approved",
        "visa_types": {"$in": ["E-7-4"]},
    }
    assert pipeline[1] == {"$project": {"embedding": 0}}
    assert pipeline[2] == {"$set": {"score": {"$meta": "vectorSearchScore"}}}


@pytest.mark.parametrize("bad", [["E-7-4", {"$ne": 1}], ["e74"], ["E-7-4; drop"], ["X"] * 11])
def test_visa_types_are_validated(bad: list[Any]) -> None:
    with pytest.raises(ValidationFailedError):
        validate_visa_types(bad)


def test_invalid_top_k_rejected() -> None:
    with pytest.raises(ValidationFailedError):
        build_pipeline(
            index_name="i", query_vector=[1.0], top_k=0, num_candidates=10, visa_types=[]
        )


def test_filter_current_drops_stale_and_unapproved_hits() -> None:
    gw = InMemoryPolicyGateway()
    gw.add(PolicyDocumentRecord(1, "v2", "a" * 64, "approved", is_current=True))
    gw.add(PolicyDocumentRecord(1, "v1", "a" * 64, "approved", is_current=False))
    hits = [_hit(1, "v2"), _hit(1, "v1"), _hit(1, "v2", active=False), _hit(2, "v1")]
    kept = filter_current(hits, gw)
    assert kept == [hits[0]]


@pytest.mark.parametrize("gateway", [UnconfiguredPolicyGateway(), InMemoryPolicyGateway(fail=True)])
def test_filter_current_fails_closed(gateway: Any) -> None:
    with pytest.raises(SearchUnavailableError):
        filter_current([_hit(1, "v1")], gateway)


class FakeCollection:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.docs = docs
        self.pipelines: list[list[dict[str, Any]]] = []

    def aggregate(self, pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.pipelines.append(pipeline)
        return self.docs


def test_english_query_uses_same_embedder_and_server_filter() -> None:
    gw = InMemoryPolicyGateway()
    gw.add(PolicyDocumentRecord(1, "v1", "a" * 64, "approved", is_current=True))
    # Simulates index lag: a pending chunk still returned by the search index.
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


class UntouchableClient:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError("activation must not touch MongoDB before checks pass")


def _approved_gateway(status: str = "approved", current: bool = True) -> InMemoryPolicyGateway:
    gw = InMemoryPolicyGateway()
    gw.add(PolicyDocumentRecord(1, "v2", "a" * 64, status, is_current=current))  # type: ignore[arg-type]
    return gw


@pytest.mark.parametrize(
    ("gateway", "expected_chunks", "sha"),
    [
        (_approved_gateway("pending"), 0, "a" * 64),
        (_approved_gateway("approved", current=False), 0, "a" * 64),
        (_approved_gateway(), 0, "b" * 64),
        (_approved_gateway(), 3, "a" * 64),
    ],
)
def test_activation_preconditions(gateway: Any, expected_chunks: int, sha: str) -> None:
    repo = FakeRepository()
    with pytest.raises(ValidationFailedError):
        activate_version(
            UntouchableClient(),  # type: ignore[arg-type]
            repo,  # type: ignore[arg-type]
            gateway,
            policy_document_id=1,
            document_version="v2",
            embedding_model="BAAI/bge-m3",
            expected_chunks=expected_chunks,
            expected_sha256=sha,
        )

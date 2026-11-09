"""Atlas Local integration: validator, unique index, real vector index, approval visibility.

Runs against ``MONGO_TEST_URI`` (default: a throwaway Atlas Local on localhost:27018) in a
fresh database that this module creates and drops. Skipped with a reason when unreachable.

    docker run -d --name hanstep-mongodb-it -p 127.0.0.1:27018:27017 \
        mongodb/mongodb-atlas-local:8.0
    uv run pytest -m "integration and rag" tests/integration
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from bson.int64 import Int64
from pymongo import MongoClient
from pymongo.errors import PyMongoError, WriteError

from app.integrations.policy_gateway import InMemoryPolicyGateway, PolicyDocumentRecord
from app.integrations.r2 import InMemoryObjectStore
from app.rag.chunking import approx_token_count
from app.rag.errors import ValidationFailedError
from app.rag.indexes import (
    create_vector_index,
    inspect_vector_index,
    load_definition,
    smoke_query,
    wait_until_queryable,
)
from app.rag.ingestion import IngestionDeps, process_entry
from app.rag.manifest import SourceEntry
from app.rag.publication import sync_policy_state
from app.rag.repository import ChunkRepository
from app.rag.retrieval import SearchService
from app.rag.schemas import ChunkRecord
from app.rag.sources import inspect_file
from tests.conftest import DATA_ROOT, FakeEmbedder, fake_vector, record_kwargs

pytestmark = [pytest.mark.integration, pytest.mark.rag]

URI = os.environ.get("MONGO_TEST_URI", "mongodb://localhost:27018/?directConnection=true")
INDEX = "rag_vector_index"
MODEL = "BAAI/bge-m3"


def eventually(check: Callable[[], bool], timeout: float = 60.0) -> bool:
    """Bounded polling for mongot replication lag."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(1.0)
    return check()


@pytest.fixture(scope="module")
def client() -> Iterator[MongoClient[dict[str, Any]]]:
    mongo: MongoClient[dict[str, Any]] = MongoClient(
        URI, serverSelectionTimeoutMS=3000, tz_aware=True
    )
    try:
        mongo.admin.command("ping")
    except PyMongoError as exc:
        pytest.skip(f"BLOCKED: Atlas Local not reachable at MONGO_TEST_URI ({type(exc).__name__})")
    yield mongo
    mongo.close()


@pytest.fixture(scope="module")
def repo(client: MongoClient[dict[str, Any]]) -> Iterator[ChunkRepository]:
    name = f"rag_it_{uuid.uuid4().hex[:10]}"
    repository = ChunkRepository(client[name], "visa_policy_chunks")
    assert repository.ensure_collection() == "created"
    assert repository.ensure_unique_index() == "created"
    definition = load_definition()
    create_vector_index(repository.collection, INDEX, definition)
    status = wait_until_queryable(repository.collection, INDEX, definition, timeout_s=240)
    if not status.queryable:
        client.drop_database(name)
        pytest.fail(f"vector index not queryable within timeout: {status}")
    yield repository
    # Only the database this module created.
    client.drop_database(name)


def _record(pid: int, version: str, index: int, text: str, **extra: Any) -> ChunkRecord:
    return ChunkRecord(
        **record_kwargs(
            policy_document_id=pid,
            document_version=version,
            chunk_index=index,
            chunk_text=text,
            embedding=fake_vector(text),
            source_sha256=extra.pop("sha", "a" * 64),
            **extra,
        )
    )


def _ids(repo: ChunkRepository, text: str, gateway: InMemoryPolicyGateway) -> list[tuple[int, str]]:
    service = SearchService(
        repo.collection, FakeEmbedder(), gateway, index_name=INDEX, num_candidates=50
    )
    return [(h.policy_document_id, h.document_version) for h in service.search(text, 5, None)]


def _raw_vector_hits(repo: ChunkRepository, text: str, pid: int) -> list[dict[str, Any]]:
    """$vectorSearch with the server filter only (no MySQL re-check), limited to one document.

    Other tests share the collection, so results are narrowed to ``pid``.
    """
    pipeline: list[dict[str, Any]] = [
        {
            "$vectorSearch": {
                "index": INDEX,
                "path": "embedding",
                "queryVector": fake_vector(text),
                "numCandidates": 50,
                "limit": 5,
                "filter": {"is_active": True, "review_status": "approved"},
            }
        },
        {"$project": {"embedding": 0}},
        {"$match": {"policy_document_id": Int64(pid)}},
    ]
    return list(repo.collection.aggregate(pipeline))


def test_index_definition_matches_and_smoke_query_runs(repo: ChunkRepository) -> None:
    status = inspect_vector_index(repo.collection, INDEX, load_definition())
    assert (status.state, status.queryable) == ("match", True)
    assert repo.validator_status()[0] == "match"
    assert repo.unique_index_status() == "match"
    assert smoke_query(repo.collection, INDEX, fake_vector("probe")) == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(policy_document_id=5),  # Int32 instead of Int64
        lambda d: d.update(embedding=d["embedding"][:1023]),
        lambda d: d.update(is_active=True),  # active while pending
        lambda d: d.update(review_status="APPROVED"),
        lambda d: d.pop("source_url"),
        lambda d: d.update(page_number=None),  # optional field must be omitted, not null
        lambda d: d.pop("source_type"),
        lambda d: d.update(source_type="NOTICE"),
    ],
)
def test_validator_rejects_contract_violations(
    repo: ChunkRepository, mutate: Callable[[dict[str, Any]], Any]
) -> None:
    doc = _record(900, "bad", 0, "검증 실패 문서").to_bson()
    mutate(doc)
    with pytest.raises(WriteError):
        repo.collection.insert_one(doc)


def test_stored_types_are_exact(repo: ChunkRepository) -> None:
    repo.insert_chunks([_record(901, "types", 0, "타입 확인", page_number=2)])
    raw = repo.collection.find_one({"policy_document_id": Int64(901)})
    assert raw is not None
    assert isinstance(raw["policy_document_id"], Int64)
    assert type(raw["chunk_index"]) is int and type(raw["page_number"]) is int
    assert raw["created_at"].tzinfo is not None
    assert repo.collection.count_documents({"policy_document_id": {"$type": "long"}}) >= 1


def test_insert_only_upsert_never_overwrites(repo: ChunkRepository) -> None:
    first = repo.insert_chunks([_record(902, "v1", 0, "원본 청크")])
    assert first.inserted == 1
    repo.collection.update_one(
        {"policy_document_id": Int64(902)},
        {"$set": {"review_status": "approved", "is_active": True}},
    )
    again = repo.insert_chunks([_record(902, "v1", 0, "다른 텍스트로 재시도")])
    assert (again.inserted, again.already_present) == (0, 1)
    stored = repo.load_version(902, "v1", MODEL)
    assert stored[0].chunk_text == "원본 청크"
    assert (stored[0].review_status, stored[0].is_active) == ("approved", True)


def _sync(repo: ChunkRepository, record: PolicyDocumentRecord, **kw: Any) -> Any:
    return sync_policy_state(repo.database.client, repo, record, embedding_model=MODEL, **kw)


def test_row_state_sync_controls_visibility(repo: ChunkRepository) -> None:
    """BE model: each version is its own policy_documents row (new id)."""
    old_id, new_id = 910, 911
    text_old, text_new = "E-7-4 추천서 제출 서류 구판", "E-7-4 추천서 제출 서류 신판"
    gateway = InMemoryPolicyGateway()
    repo.insert_chunks([_record(old_id, "v1", 0, text_old, sha="1" * 64)])
    repo.insert_chunks([_record(new_id, "v2", 0, text_new, sha="2" * 64)])

    # DRAFT: nothing searchable even after mongot catches up.
    draft = PolicyDocumentRecord(old_id, "v1", "1" * 64, "DRAFT")
    assert _sync(repo, draft).action == "unchanged"
    time.sleep(3)
    assert _raw_vector_hits(repo, text_old, old_id) == []

    # APPROVED + searchable: activated, and a repeated sync is a no-op.
    approved_old = PolicyDocumentRecord(old_id, "v1", "1" * 64, "APPROVED", searchable=True)
    gateway.add(approved_old)
    first = _sync(repo, approved_old, expected_chunks=1)
    assert (first.action, first.modified, first.is_active) == ("activated", 1, True)
    assert _sync(repo, approved_old, expected_chunks=1).action == "unchanged"
    assert eventually(lambda: _ids(repo, text_old, gateway) == [(old_id, "v1")])

    # Approved but not yet in force (searchable=false): stays hidden.
    not_in_force = PolicyDocumentRecord(new_id, "v2", "2" * 64, "APPROVED", searchable=False)
    # Review status moves pending -> approved, but nothing is switched on.
    assert _sync(repo, not_in_force).action == "updated"
    assert _sync(repo, not_in_force).action == "unchanged"
    new_chunk = repo.load_version(new_id, "v2", MODEL)[0]
    assert (new_chunk.review_status, new_chunk.is_active) == ("approved", False)

    # Switch-over is two row syncs: old row RETIRED, new row searchable.
    retired_old = PolicyDocumentRecord(old_id, "v1", "1" * 64, "RETIRED")
    live_new = PolicyDocumentRecord(new_id, "v2", "2" * 64, "APPROVED", searchable=True)
    gateway.add(retired_old)
    gateway.add(live_new)
    assert _sync(repo, retired_old).action == "deactivated"
    assert _sync(repo, live_new, expected_chunks=1).action == "activated"
    old_chunk = repo.load_version(old_id, "v1", MODEL)[0]
    assert (old_chunk.review_status, old_chunk.is_active) == ("approved", False)
    assert eventually(lambda: _ids(repo, text_new, gateway) == [(new_id, "v2")])
    assert eventually(lambda: _raw_vector_hits(repo, text_old, old_id) == [])

    # Back to NEEDS_REVIEW: hidden again.
    review = PolicyDocumentRecord(new_id, "v2", "2" * 64, "NEEDS_REVIEW")
    assert _sync(repo, review).action == "deactivated"
    assert eventually(lambda: _raw_vector_hits(repo, text_new, new_id) == [])


def test_failed_precondition_leaves_state_untouched(repo: ChunkRepository) -> None:
    pid = 912
    repo.insert_chunks([_record(pid, "v1", 0, "해시 불일치 문서", sha="3" * 64)])
    wrong_hash = PolicyDocumentRecord(pid, "v1", "4" * 64, "APPROVED", searchable=True)
    with pytest.raises(ValidationFailedError):
        _sync(repo, wrong_hash)
    chunk = repo.load_version(pid, "v1", MODEL)[0]
    assert (chunk.review_status, chunk.is_active) == ("pending", False)


def test_other_versions_under_same_id_are_deactivated(repo: ChunkRepository) -> None:
    pid = 913
    repo.insert_chunks([_record(pid, "stale", 0, "MySQL에 없는 버전", sha="5" * 64)])
    repo.collection.update_many(
        {"policy_document_id": Int64(pid)},
        {"$set": {"review_status": "approved", "is_active": True}},
    )
    repo.insert_chunks([_record(pid, "current", 0, "MySQL의 현재 버전", sha="6" * 64)])
    result = _sync(
        repo, PolicyDocumentRecord(pid, "current", "6" * 64, "APPROVED", searchable=True)
    )
    assert (result.action, result.stale_deactivated) == ("activated", 1)
    assert repo.load_version(pid, "stale", MODEL)[0].is_active is False


def test_approved_row_without_chunks_reports_not_ingested(repo: ChunkRepository) -> None:
    result = _sync(repo, PolicyDocumentRecord(914, "v1", "7" * 64, "APPROVED", searchable=True))
    assert (result.action, result.is_active) == ("not_ingested", False)


def test_stale_index_results_are_filtered_by_mysql_state(repo: ChunkRepository) -> None:
    pid, text = 920, "구 버전이 인덱스에 남은 경우"
    repo.insert_chunks([_record(pid, "old", 0, text)])
    repo.collection.update_many(
        {"policy_document_id": Int64(pid)},
        {"$set": {"review_status": "approved", "is_active": True}},
    )
    assert eventually(
        lambda: any(h["policy_document_id"] == pid for h in _raw_vector_hits(repo, text, pid))
    )
    # MySQL says another version is current: the search service must drop the hit.
    gateway = InMemoryPolicyGateway()
    gateway.add(PolicyDocumentRecord(pid, "new", "a" * 64, "APPROVED", searchable=True))
    assert (pid, "old") not in _ids(repo, text, gateway)


def test_real_document_ingestion_is_idempotent(repo: ChunkRepository) -> None:
    path = "visa/recommendations/central/mafra/e74_mafra_2026_20260305.pdf"
    sha = inspect_file(DATA_ROOT, path).sha256
    entry = SourceEntry.model_validate(
        {
            "path": path,
            "domain": "visa",
            "sha256": sha,
            "title": "통합 테스트용 제목",
            "source_url": "https://www.mafra.go.kr/it",
            "source_org": "농림축산식품부",
            "visa_types": ["E-7-4"],
            "source_type": "notice",
            "policy_document_id": 930,
            "document_version": "it-1",
            "review": {"verified": True},
        }
    )
    gateway = InMemoryPolicyGateway()
    gateway.add(PolicyDocumentRecord(930, "it-1", sha, "DRAFT"))
    embedder = FakeEmbedder()
    deps = IngestionDeps(
        repository=repo,
        gateway=gateway,
        object_stores={"visa": InMemoryObjectStore()},
        embedder=embedder,
        token_counter=approx_token_count,
        tokenizer_id="approx",
        r2_prefix="policy-sources",
        embedding_model=MODEL,
        clock=lambda: datetime(2026, 11, 8, tzinfo=UTC),
    )
    first = process_entry(entry, DATA_ROOT, deps, apply=True)
    assert first.status == "loaded", first.reasons
    stored = repo.load_version(930, "it-1", MODEL)
    assert len(stored) == first.chunks
    assert all(r.review_status == "pending" and not r.is_active for r in stored)
    assert all(r.page_number for r in stored)

    calls = embedder.embedded_texts
    second = process_entry(entry, DATA_ROOT, deps, apply=True)
    assert second.status == "skipped"
    assert embedder.embedded_texts == calls
    assert repo.collection.count_documents({"policy_document_id": Int64(930)}) == first.chunks
    assert repo.find_duplicates() == []

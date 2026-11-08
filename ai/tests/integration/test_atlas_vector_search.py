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
from app.rag.publication import activate_version, deactivate_version
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


def test_pending_hidden_then_activation_switches_versions(repo: ChunkRepository) -> None:
    client = repo.database.client
    pid, text_v1, text_v2 = 910, "E-7-4 추천서 제출 서류 v1", "E-7-4 추천서 제출 서류 v2"
    gateway = InMemoryPolicyGateway()
    repo.insert_chunks([_record(pid, "v1", 0, text_v1, sha="1" * 64)])

    # Pending chunks never appear, even after mongot catches up.
    time.sleep(3)
    assert _raw_vector_hits(repo, text_v1, pid) == []

    gateway.add(PolicyDocumentRecord(pid, "v1", "1" * 64, "approved", is_current=True))
    activate_version(
        client,
        repo,
        gateway,
        policy_document_id=pid,
        document_version="v1",
        embedding_model=MODEL,
        expected_chunks=1,
        expected_sha256="1" * 64,
    )
    assert eventually(lambda: _ids(repo, text_v1, gateway) == [(pid, "v1")])

    # New version arrives pending: v1 stays the only searchable version.
    repo.insert_chunks([_record(pid, "v2", 0, text_v2, sha="2" * 64)])
    gateway.add(PolicyDocumentRecord(pid, "v2", "2" * 64, "pending", is_current=False))
    with pytest.raises(ValidationFailedError):
        activate_version(
            client,
            repo,
            gateway,
            policy_document_id=pid,
            document_version="v2",
            embedding_model=MODEL,
            expected_chunks=1,
            expected_sha256="2" * 64,
        )
    time.sleep(3)
    assert {(h["document_version"]) for h in _raw_vector_hits(repo, text_v2, pid)} == {"v1"}

    # MySQL approves v2 and marks it current; switch-over deactivates v1 atomically.
    gateway.add(PolicyDocumentRecord(pid, "v1", "1" * 64, "approved", is_current=False))
    gateway.add(PolicyDocumentRecord(pid, "v2", "2" * 64, "approved", is_current=True))
    result = activate_version(
        client,
        repo,
        gateway,
        policy_document_id=pid,
        document_version="v2",
        embedding_model=MODEL,
        expected_chunks=1,
        expected_sha256="2" * 64,
    )
    assert (result.activated, result.deactivated) == (1, 1)
    v1 = repo.load_version(pid, "v1", MODEL)[0]
    assert (v1.review_status, v1.is_active) == ("approved", False)
    assert eventually(lambda: _ids(repo, text_v2, gateway) == [(pid, "v2")])

    # Revocation hides the version again.
    deactivate_version(
        client, repo, policy_document_id=pid, document_version="v2", review_status="rejected"
    )
    assert eventually(lambda: _raw_vector_hits(repo, text_v2, pid) == [])


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
    gateway.add(PolicyDocumentRecord(pid, "new", "a" * 64, "approved", is_current=True))
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
            "policy_document_id": 930,
            "document_version": "it-1",
            "review": {"verified": True},
        }
    )
    gateway = InMemoryPolicyGateway()
    gateway.add(PolicyDocumentRecord(930, "it-1", sha, "pending", is_current=False))
    embedder = FakeEmbedder()
    deps = IngestionDeps(
        repository=repo,
        gateway=gateway,
        object_store=InMemoryObjectStore(),
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

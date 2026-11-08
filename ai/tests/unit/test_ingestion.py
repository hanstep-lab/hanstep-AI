from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.integrations.policy_gateway import (
    InMemoryPolicyGateway,
    PolicyDocumentRecord,
    UnconfiguredPolicyGateway,
)
from app.integrations.r2 import InMemoryObjectStore, UnconfiguredObjectStore, object_key
from app.rag.chunking import approx_token_count
from app.rag.errors import VersionConflictError
from app.rag.ingestion import IngestionDeps, process_entry, seoul_midnight_utc
from app.rag.manifest import SourceEntry
from app.rag.repository import ExistingChunk, plan_version_write
from app.rag.sources import inspect_file
from tests.conftest import DATA_ROOT, FakeEmbedder, FakeRepository

pytestmark = pytest.mark.unit

PATH = "visa/recommendations/central/mafra/e74_mafra_2026_20260305.pdf"
SHA = inspect_file(DATA_ROOT, PATH).sha256


def _entry(**overrides: Any) -> SourceEntry:
    base: dict[str, Any] = {
        "path": PATH,
        "domain": "visa",
        "sha256": SHA,
        "title": "2026년 농축산분야 숙련기능인력(E-7-4) 고용추천 운영방안 공고",
        "source_url": "https://www.mafra.go.kr/example",
        "source_org": "농림축산식품부",
        "visa_types": ["E-7-4"],
        "policy_document_id": 11,
        "document_version": "2026-163",
        "review": {"verified": True},
    }
    base.update(overrides)
    return SourceEntry.model_validate(base)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 8, 1, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(minutes=1)
        return self.now


@pytest.fixture
def gateway() -> InMemoryPolicyGateway:
    gw = InMemoryPolicyGateway()
    gw.add(PolicyDocumentRecord(11, "2026-163", SHA, "pending", is_current=False))
    return gw


def _deps(repo: FakeRepository, embedder: FakeEmbedder, gateway: Any, store: Any) -> IngestionDeps:
    return IngestionDeps(
        repository=repo,  # type: ignore[arg-type]
        gateway=gateway,
        object_store=store,
        embedder=embedder,
        token_counter=approx_token_count,
        tokenizer_id="approx",
        r2_prefix="policy-sources",
        embedding_model="BAAI/bge-m3",
        clock=Clock(),
    )


def test_dry_run_performs_no_external_writes(
    fake_repository: FakeRepository, fake_embedder: FakeEmbedder, gateway: InMemoryPolicyGateway
) -> None:
    store = InMemoryObjectStore()
    result = process_entry(
        _entry(), DATA_ROOT, _deps(fake_repository, fake_embedder, gateway, store), apply=False
    )
    assert result.status == "planned"
    assert result.chunks > 0
    assert store.puts == 0
    assert fake_repository.writes == 0
    assert fake_embedder.document_calls == 0
    assert gateway.r2_keys == {}


def test_apply_loads_pending_then_rerun_is_noop_and_preserves_approval(
    fake_repository: FakeRepository, fake_embedder: FakeEmbedder, gateway: InMemoryPolicyGateway
) -> None:
    store = InMemoryObjectStore()
    deps = _deps(fake_repository, fake_embedder, gateway, store)
    first = process_entry(_entry(), DATA_ROOT, deps, apply=True)
    assert first.status == "loaded", first.reasons
    assert first.inserted == first.chunks == first.embedded
    stored = fake_repository.load_version(11, "2026-163", "BAAI/bge-m3")
    assert all(r.review_status == "pending" and not r.is_active for r in stored)
    assert all(r.page_number is not None for r in stored)
    key = object_key("policy-sources", "visa", SHA, ".pdf")
    assert store.objects[key] == (DATA_ROOT / PATH).read_bytes()
    assert gateway.r2_keys[(11, "2026-163")] == key

    created = {r.chunk_index: r.created_at for r in stored}
    fake_repository.set_state(11, "2026-163", "approved", True)
    calls_before = fake_embedder.embedded_texts
    second = process_entry(_entry(), DATA_ROOT, deps, apply=True)
    assert second.status == "skipped"
    assert fake_embedder.embedded_texts == calls_before  # zero embedding calls
    after = fake_repository.load_version(11, "2026-163", "BAAI/bge-m3")
    assert all(r.review_status == "approved" and r.is_active for r in after)
    assert {r.chunk_index: r.created_at for r in after} == created
    assert store.puts == 1


def test_partial_failure_resumes_without_duplicates(
    fake_repository: FakeRepository, fake_embedder: FakeEmbedder, gateway: InMemoryPolicyGateway
) -> None:
    deps = _deps(fake_repository, fake_embedder, gateway, InMemoryObjectStore())
    fake_repository.fail_after = 3
    failed = process_entry(_entry(), DATA_ROOT, deps, apply=True)
    assert failed.status == "failed"
    assert len(fake_repository.docs) == 3

    fake_repository.fail_after = None
    embedded_before = fake_embedder.embedded_texts
    resumed = process_entry(_entry(), DATA_ROOT, deps, apply=True)
    assert resumed.status == "loaded", resumed.reasons
    assert resumed.embedded == resumed.chunks - 3
    assert fake_embedder.embedded_texts - embedded_before == resumed.chunks - 3
    assert len(fake_repository.docs) == resumed.chunks


def test_mysql_hash_mismatch_fails(
    fake_repository: FakeRepository, fake_embedder: FakeEmbedder
) -> None:
    gw = InMemoryPolicyGateway()
    gw.add(PolicyDocumentRecord(11, "2026-163", "b" * 64, "pending", is_current=False))
    result = process_entry(
        _entry(),
        DATA_ROOT,
        _deps(fake_repository, fake_embedder, gw, InMemoryObjectStore()),
        apply=True,
    )
    assert result.status == "failed"
    assert fake_repository.writes == 0


def test_unconfigured_contracts_block_without_writes(
    fake_repository: FakeRepository, fake_embedder: FakeEmbedder
) -> None:
    deps = _deps(
        fake_repository, fake_embedder, UnconfiguredPolicyGateway(), UnconfiguredObjectStore()
    )
    result = process_entry(_entry(), DATA_ROOT, deps, apply=True)
    assert result.status == "blocked"
    assert "policy_documents" in result.reasons[0]
    assert fake_repository.writes == 0 and fake_embedder.document_calls == 0


def test_finance_is_blocked_and_never_written(
    fake_repository: FakeRepository, fake_embedder: FakeEmbedder, gateway: InMemoryPolicyGateway
) -> None:
    path = "finance/shinhan/sol_global_savings.pdf"
    entry = _entry(path=path, domain="finance", sha256=inspect_file(DATA_ROOT, path).sha256)
    store = InMemoryObjectStore()
    result = process_entry(
        entry, DATA_ROOT, _deps(fake_repository, fake_embedder, gateway, store), apply=True
    )
    assert result.status == "blocked"
    assert any("finance" in r for r in result.reasons)
    assert fake_repository.writes == 0 and store.puts == 0


def test_hwp_is_extracted_directly_without_page_numbers(
    fake_repository: FakeRepository, fake_embedder: FakeEmbedder, gateway: InMemoryPolicyGateway
) -> None:
    path = "visa/guides/e74_central_agency_guide_2026.hwp"
    sha = inspect_file(DATA_ROOT, path).sha256
    gateway.add(PolicyDocumentRecord(12, "v1", sha, "pending", is_current=False))
    entry = _entry(path=path, sha256=sha, policy_document_id=12, document_version="v1")
    result = process_entry(
        entry,
        DATA_ROOT,
        _deps(fake_repository, fake_embedder, gateway, InMemoryObjectStore()),
        apply=True,
    )
    assert result.status == "loaded", result.reasons
    assert result.chunks_with_page == 0
    stored = fake_repository.load_version(12, "v1", "BAAI/bge-m3")
    assert stored and all(r.page_number is None for r in stored)


def test_plan_same_version_different_hash_is_hard_error() -> None:
    existing = [ExistingChunk(0, "a", "a" * 64)]
    with pytest.raises(VersionConflictError):
        plan_version_write(existing, source_sha256="b" * 64, expected_texts=["a"])


def test_plan_changed_chunking_requires_reprocessing_policy() -> None:
    existing = [ExistingChunk(0, "old text", "a" * 64)]
    with pytest.raises(VersionConflictError):
        plan_version_write(existing, source_sha256="a" * 64, expected_texts=["new text"])
    with pytest.raises(VersionConflictError):
        plan_version_write(
            [*existing, ExistingChunk(1, "x", "a" * 64)],
            source_sha256="a" * 64,
            expected_texts=["old text"],
        )


def test_plan_noop_resume_insert() -> None:
    stored = [ExistingChunk(0, "a", "a" * 64)]
    assert plan_version_write(stored, source_sha256="a" * 64, expected_texts=["a"]).action == "noop"
    plan = plan_version_write(stored, source_sha256="a" * 64, expected_texts=["a", "b"])
    assert (plan.action, plan.missing_indexes) == ("resume", (1,))
    assert (
        plan_version_write([], source_sha256="a" * 64, expected_texts=["a"]).action == "insert_all"
    )


def test_effective_dates_are_kst_midnight() -> None:
    from datetime import date

    assert seoul_midnight_utc(date(2026, 6, 1)) == datetime(2026, 5, 31, 15, 0, tzinfo=UTC)

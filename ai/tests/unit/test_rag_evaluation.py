"""검색 평가 계산(app.rag.evaluation) 단위 테스트. MongoDB·Spring·모델 없이 실행한다."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.integrations.spring_policy_gateway import SpringPolicyGateway
from app.rag.errors import ContractBlockedError
from app.rag.evaluation import (
    by_language,
    case_counts,
    corpus_snapshot,
    effective_num_candidates,
    invalid_kinds,
    latency_summary,
    run_cases,
    score_case,
    select_gateway,
)
from app.rag.retrieval import SearchHit, SearchUnavailableError

pytestmark = pytest.mark.unit

MODEL = "BAAI/bge-m3"


def _hit(doc_id: int, version: str, index: int, score: float = 0.5) -> SearchHit:
    return SearchHit(
        policy_document_id=doc_id,
        document_version=version,
        chunk_index=index,
        chunk_text="본문",
        title=f"문서{doc_id}",
        source_url="https://example.org",
        source_org="법무부",
        visa_types=["E-7-4"],
        score=score,
    )


class FakeCollection:
    """aggregate 파이프라인을 기록하고 정해 둔 그룹 결과를 돌려주는 가짜 컬렉션."""

    def __init__(self, groups: list[dict[str, Any]]) -> None:
        self.groups = groups
        self.pipelines: list[list[dict[str, Any]]] = []

    def aggregate(self, pipeline: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
        self.pipelines.append(pipeline)
        return iter(self.groups)


class FakeGateway:
    """문서별 현재 버전을 돌려주거나 정해 둔 오류를 내는 가짜 승인 확인 게이트웨이."""

    def __init__(self, current: dict[int, str], error: Exception | None = None) -> None:
        self.current = current
        self.error = error
        self.calls: list[set[int]] = []

    def current_versions(self, policy_document_ids: Iterable[int]) -> dict[int, str]:
        ids = set(policy_document_ids)
        self.calls.append(ids)
        if self.error is not None:
            raise self.error
        return {i: v for i, v in self.current.items() if i in ids}


def _group(doc_id: int, version: str, chunks: int) -> dict[str, Any]:
    return {"_id": {"policy_document_id": doc_id, "document_version": version}, "chunks": chunks}


# --- 게이트웨이 선택 ----------------------------------------------------------------------


def test_select_gateway_uses_spring_when_configured() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        spring_internal_api_url="http://spring:8080",
        spring_internal_api_key=SecretStr("s" * 40),
    )
    gateway = select_gateway(settings)
    assert isinstance(gateway, SpringPolicyGateway)
    gateway.close()


def test_select_gateway_is_none_without_spring() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        spring_internal_api_url="",
        spring_internal_api_key=SecretStr(""),
    )
    assert select_gateway(settings) is None


# --- 측정 조건 ----------------------------------------------------------------------------


def test_corpus_snapshot_counts_approved_active_chunks_and_marks_current_versions() -> None:
    collection = FakeCollection([_group(7, "v1", 10), _group(6, "v2", 41), _group(1, "v1", 5)])
    gateway = FakeGateway({1: "v1", 6: "v2"})

    corpus = corpus_snapshot(collection, gateway, embedding_model=MODEL)  # type: ignore[arg-type]

    match = collection.pipelines[0][0]["$match"]
    assert match == {"review_status": "approved", "is_active": True, "embedding_model": MODEL}
    assert gateway.calls == [{1, 6, 7}]
    assert corpus["documents"] == [
        {"policy_document_id": 1, "document_version": "v1", "chunks": 5, "current": True},
        {"policy_document_id": 6, "document_version": "v2", "chunks": 41, "current": True},
        {"policy_document_id": 7, "document_version": "v1", "chunks": 10, "current": False},
    ]
    assert corpus["searchable_document_ids"] == [1, 6]
    assert corpus["searchable_chunks"] == 46


def test_corpus_snapshot_skips_gateway_when_nothing_is_approved() -> None:
    gateway = FakeGateway({})
    corpus = corpus_snapshot(
        FakeCollection([]),
        gateway,  # type: ignore[arg-type]
        embedding_model=MODEL,
    )
    assert gateway.calls == []
    assert corpus == {"documents": [], "searchable_document_ids": [], "searchable_chunks": 0}


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (ContractBlockedError("contract unconfirmed"), "contract unconfirmed"),
        (RuntimeError("connection refused"), "approval state check failed"),
    ],
)
def test_corpus_snapshot_reports_approval_check_failure(error: Exception, message: str) -> None:
    gateway = FakeGateway({}, error=error)
    with pytest.raises(SearchUnavailableError, match=message):
        corpus_snapshot(
            FakeCollection([_group(6, "v2", 3)]),
            gateway,  # type: ignore[arg-type]
            embedding_model=MODEL,
        )


def test_case_counts_by_language_and_kind() -> None:
    cases = [
        {"language": "ko", "kind": "answerable"},
        {"language": "ko", "kind": "unanswerable"},
        {"language": "en"},
    ]
    assert case_counts(cases) == {
        "by_language": {"en": 1, "ko": 2},
        "by_kind": {"answerable": 2, "unanswerable": 1},
    }


def test_effective_num_candidates_matches_search_service() -> None:
    assert effective_num_candidates(100, 5) == 100
    assert effective_num_candidates(100, 8) == 160


# --- 채점과 unanswerable 분리 --------------------------------------------------------------


def test_score_case_recall_and_reciprocal_rank() -> None:
    gold = [{"policy_document_id": 6, "document_version": "v2", "chunk_index": 3}]
    hits = [(1, "v1", 0), (6, "v2", 3)]
    assert score_case(hits, gold, 5) == (1.0, 0.5)
    assert score_case(hits, gold, 1) == (0.0, 0.0)


def test_run_cases_separates_unanswerable_from_scoring() -> None:
    results = {
        "답 있음": [_hit(1, "v1", 0, 0.7), _hit(6, "v2", 3, 0.6)],
        "답 없음": [_hit(5, "v1", 2, 0.81), _hit(1, "v1", 0, 0.4)],
        "결과 없음": [],
    }
    seen: list[tuple[str, int, list[str] | None]] = []

    def search(query: str, top_k: int, visa_types: list[str] | None) -> list[SearchHit]:
        seen.append((query, top_k, visa_types))
        return results[query]

    ticks = iter([0.0, 0.010, 1.0, 1.020, 2.0, 2.030])
    cases: list[dict[str, Any]] = [
        {
            "id": "ko-01",
            "language": "ko",
            "kind": "answerable",
            "query": "답 있음",
            "visa_types": ["E-7-4"],
            "gold": [{"policy_document_id": 6, "document_version": "v2", "chunk_index": 3}],
        },
        {"id": "ko-n1", "language": "ko", "kind": "unanswerable", "query": "답 없음"},
        {"id": "en-n1", "language": "en", "kind": "unanswerable", "query": "결과 없음"},
    ]

    rows, unanswerable, latencies = run_cases(search, cases, 5, clock=lambda: next(ticks))

    assert seen == [("답 있음", 5, ["E-7-4"]), ("답 없음", 5, None), ("결과 없음", 5, None)]
    assert [r["id"] for r in rows] == ["ko-01"]
    assert (rows[0]["recall"], rows[0]["rr"]) == (1.0, 0.5)
    assert unanswerable == [
        {
            "id": "ko-n1",
            "language": "ko",
            "top1_score": 0.81,
            "top1_policy_document_id": 5,
            "hits": [(5, "v1", 2), (1, "v1", 0)],
        },
        {
            "id": "en-n1",
            "language": "en",
            "top1_score": None,
            "top1_policy_document_id": None,
            "hits": [],
        },
    ]
    assert latencies == pytest.approx([10.0, 20.0, 30.0])


@pytest.mark.parametrize("kind", ["Unanswerable", "unanswerable ", "out_of_scope", None])
def test_run_cases_rejects_unknown_kind_before_searching(kind: str | None) -> None:
    def search(query: str, top_k: int, visa_types: list[str] | None) -> list[SearchHit]:
        raise AssertionError("must not search")

    cases: list[dict[str, Any]] = [
        {"id": "a", "language": "ko", "kind": "answerable", "query": "q"},
        {"id": "b", "language": "ko", "kind": kind, "query": "r"},
    ]
    with pytest.raises(ValueError, match="'b'"):
        run_cases(search, cases, 5)


def test_invalid_kinds_lists_cases_outside_answerable_unanswerable() -> None:
    cases = [
        {"id": "a", "kind": "answerable"},
        {"id": "b", "kind": "unanswerable"},
        {"id": "c"},
        {"id": "d", "kind": "Unanswerable"},
    ]
    assert invalid_kinds(cases) == [{"id": "d", "kind": "Unanswerable"}]


def test_run_cases_propagates_search_unavailable() -> None:
    def search(query: str, top_k: int, visa_types: list[str] | None) -> list[SearchHit]:
        raise SearchUnavailableError("approval state check failed")

    with pytest.raises(SearchUnavailableError):
        run_cases(search, [{"id": "a", "language": "ko", "query": "q"}], 5)


def test_by_language_uses_scored_rows_only() -> None:
    rows = [
        {"language": "ko", "recall": 1.0, "rr": 1.0},
        {"language": "ko", "recall": 0.0, "rr": 0.0},
        {"language": "en", "recall": 1.0, "rr": 0.5},
    ]
    assert by_language(rows) == {
        "en": {"n": 1, "recall@k": 1.0, "mrr@k": 0.5},
        "ko": {"n": 2, "recall@k": 0.5, "mrr@k": 0.5},
    }


def test_latency_summary() -> None:
    assert latency_summary([]) == {"p50": None, "p95": None}
    summary = latency_summary([30.0, 10.0, 20.0, 40.0])
    assert summary["p50"] == 25.0
    assert summary["p95"] == 40.0


@pytest.mark.parametrize(
    ("latencies", "p95"),
    [
        ([7.0], 7.0),
        ([10.0, 1000.0], 1000.0),
        ([float(i) for i in range(1, 11)], 10.0),
        ([float(i) for i in range(1, 21)], 19.0),
        ([float(i) for i in range(1, 26)], 24.0),
    ],
)
def test_latency_summary_p95_is_nearest_rank(latencies: list[float], p95: float) -> None:
    assert latency_summary(list(reversed(latencies)))["p95"] == p95

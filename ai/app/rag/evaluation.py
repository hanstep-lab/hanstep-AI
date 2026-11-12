"""검색 평가 계산: 점수, 측정 조건, 답이 없어야 하는 질문의 상위 결과(scripts/evaluate_rag.py).

평가는 운영 검색과 같은 경로(SearchService + Spring 승인 확인)를 거친다. 승인 확인을 건너뛰는
평가 전용 경로는 두지 않는다. DB·네트워크 연결은 스크립트가 만들고, 여기서는 받은 객체만 쓴다.
"""

from __future__ import annotations

import statistics
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from app.core.config import Settings
from app.integrations.policy_gateway import PolicyDocumentGateway
from app.integrations.spring_policy_gateway import SpringPolicyGateway
from app.rag.errors import ContractBlockedError
from app.rag.retrieval import CANDIDATES_PER_RESULT, SearchHit, SearchUnavailableError

UNANSWERABLE = "unanswerable"

SearchFn = Callable[[str, int, list[str] | None], list[SearchHit]]


def select_gateway(settings: Settings) -> PolicyDocumentGateway | None:
    """운영 검색(app.main)과 같은 승인 확인 게이트웨이. Spring 설정이 없으면 None(평가 불가)."""
    if not settings.spring_configured():
        return None
    return SpringPolicyGateway(
        base_url=settings.spring_internal_api_url,
        api_key=settings.spring_internal_api_key.get_secret_value(),
        header=settings.internal_token_header,
    )


def effective_num_candidates(setting: int, top_k: int) -> int:
    """검색이 실제로 쓰는 numCandidates(SearchService와 같은 계산)."""
    return max(setting, top_k * CANDIDATES_PER_RESULT)


def score_case(
    hits: list[tuple[int, str, int]], gold: list[dict[str, Any]], k: int
) -> tuple[float, float]:
    """Recall@k(상위 k개 안에 정답 청크가 하나라도 있는지)와 첫 정답의 역순위(reciprocal rank)."""
    gold_keys = {(g["policy_document_id"], g["document_version"], g["chunk_index"]) for g in gold}
    for rank, hit in enumerate(hits[:k], start=1):
        if hit in gold_keys:
            return 1.0, 1.0 / rank
    return 0.0, 0.0


def corpus_snapshot(collection: Any, gateway: PolicyDocumentGateway) -> dict[str, Any]:
    """평가 시점의 검색 대상: MongoDB 승인·활성 청크를 문서·버전별로 세고 Spring 현재 버전과 맞춘다.

    ``current``가 true인 문서만 검색 결과로 나올 수 있다. 승인 확인에 실패하면
    SearchUnavailableError를 올린다.
    """
    groups = collection.aggregate(
        [
            {"$match": {"review_status": "approved", "is_active": True}},
            {
                "$group": {
                    "_id": {
                        "policy_document_id": "$policy_document_id",
                        "document_version": "$document_version",
                    },
                    "chunks": {"$sum": 1},
                }
            },
        ]
    )
    rows: list[dict[str, Any]] = sorted(
        (
            {
                "policy_document_id": int(g["_id"]["policy_document_id"]),
                "document_version": str(g["_id"]["document_version"]),
                "chunks": int(g["chunks"]),
            }
            for g in groups
        ),
        key=lambda r: (r["policy_document_id"], r["document_version"]),
    )
    current: dict[int, str] = {}
    if rows:
        # 검색 경로(retrieval.filter_current)와 같이 승인 확인 실패는 평가 불가로 올린다.
        try:
            current = gateway.current_versions({r["policy_document_id"] for r in rows})
        except ContractBlockedError as exc:
            raise SearchUnavailableError(str(exc)) from exc
        except Exception as exc:
            raise SearchUnavailableError("approval state check failed") from exc
    documents = [
        {**r, "current": current.get(r["policy_document_id"]) == r["document_version"]}
        for r in rows
    ]
    searchable = [d for d in documents if d["current"]]
    return {
        "documents": documents,
        "searchable_document_ids": [d["policy_document_id"] for d in searchable],
        "searchable_chunks": sum(d["chunks"] for d in searchable),
    }


def case_counts(cases: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    """평가한 질문 수(언어별·kind별)."""
    items = list(cases)
    return {
        "by_language": dict(sorted(Counter(str(c.get("language")) for c in items).items())),
        "by_kind": dict(sorted(Counter(_kind(c) for c in items).items())),
    }


def _kind(case: Mapping[str, Any]) -> str:
    return str(case.get("kind", "answerable"))


def run_cases(
    search: SearchFn,
    cases: Sequence[Mapping[str, Any]],
    top_k: int,
    *,
    clock: Callable[[], float] = time.perf_counter,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[float]]:
    """질문마다 검색해 (채점 행, 답이 없어야 하는 질문의 상위 1위, 지연 ms)를 돌려준다.

    ``kind``가 unanswerable인 질문은 Recall·MRR에서 빼고, 상위 1위 점수와 문서 id만 남긴다.
    판정 기준은 두지 않는다. 검색 실패(SearchUnavailableError)는 그대로 올린다.
    """
    rows: list[dict[str, Any]] = []
    unanswerable: list[dict[str, Any]] = []
    latencies: list[float] = []
    for case in cases:
        start = clock()
        hits = search(case["query"], top_k, case.get("visa_types"))
        latencies.append((clock() - start) * 1000)
        keys = [(h.policy_document_id, h.document_version, h.chunk_index) for h in hits]
        if _kind(case) == UNANSWERABLE:
            top = hits[0] if hits else None
            unanswerable.append(
                {
                    "id": case["id"],
                    "language": case.get("language"),
                    "top1_score": top.score if top else None,
                    "top1_policy_document_id": top.policy_document_id if top else None,
                    "hits": keys,
                }
            )
            continue
        recall, rr = score_case(keys, case.get("gold", []), top_k)
        rows.append(
            {
                "id": case["id"],
                "language": case.get("language"),
                "kind": _kind(case),
                "recall": recall,
                "rr": rr,
                "hits": keys,
            }
        )
    return rows, unanswerable, latencies


def by_language(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, float]]:
    """언어별 n, Recall@k, MRR@k(채점 행만)."""
    result: dict[str, dict[str, float]] = {}
    for lang in sorted({str(r["language"]) for r in rows}):
        subset = [r for r in rows if str(r["language"]) == lang]
        result[lang] = {
            "n": len(subset),
            "recall@k": statistics.mean(r["recall"] for r in subset),
            "mrr@k": statistics.mean(r["rr"] for r in subset),
        }
    return result


def latency_summary(latencies: Sequence[float]) -> dict[str, float | None]:
    """검색 지연 p50·p95(ms). 값이 없으면 None."""
    if not latencies:
        return {"p50": None, "p95": None}
    ordered = sorted(latencies)
    return {
        "p50": statistics.median(ordered),
        "p95": ordered[max(0, int(len(ordered) * 0.95) - 1)],
    }

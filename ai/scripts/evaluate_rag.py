"""검색 평가: 언어별 Recall@5, MRR@5, 누출 검사, 지연 시간.

정답(gold)은 검증된 한국어 원문을 보고 사람이 작성해야 한다. ``status != "ready"``인
케이스는 blocked로 보고하며, 절대 통과로 보고하지 않는다.

    uv run python scripts/evaluate_rag.py --cases specs/rag/evaluation.json \
        --output artifacts/rag/evaluation.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401

from app.core.config import get_settings
from app.integrations.policy_gateway import UnconfiguredPolicyGateway
from app.rag.cli import EXIT_BLOCKED, EXIT_FAILED, EXIT_OK, print_json, write_json
from app.rag.embeddings import BgeM3Embedder
from app.rag.retrieval import SearchService, SearchUnavailableError


def score_case(
    hits: list[tuple[int, str, int]], gold: list[dict[str, Any]], k: int
) -> tuple[float, float]:
    """Recall@k(상위 k개 안에 정답 청크가 하나라도 있는지)와 첫 정답의 역순위(reciprocal rank)."""
    gold_keys = {(g["policy_document_id"], g["document_version"], g["chunk_index"]) for g in gold}
    for rank, hit in enumerate(hits[:k], start=1):
        if hit in gold_keys:
            return 1.0, 1.0 / rank
    return 0.0, 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("artifacts/rag/evaluation.json"))
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    spec = json.loads(args.cases.read_text(encoding="utf-8"))
    cases = spec.get("cases", [])
    ready = [c for c in cases if c.get("status") == "ready"]
    blocked = [
        {"id": c.get("id"), "reason": c.get("blocked_reason", c.get("status"))}
        for c in cases
        if c.get("status") != "ready"
    ]
    settings = get_settings()
    base = {"cases_total": len(cases), "ready": len(ready), "blocked": blocked}
    if not ready:
        print_json({**base, "result": "BLOCKED: no human-verified gold cases are ready"})
        write_json(args.output, {**base, "result": "blocked"})
        return EXIT_BLOCKED
    if not settings.mongo_configured():
        print("BLOCKED: MONGO_URI is not configured")
        return EXIT_BLOCKED

    from pymongo import MongoClient

    client: MongoClient[dict[str, Any]] = MongoClient(
        settings.mongo_uri.get_secret_value(), serverSelectionTimeoutMS=5000, tz_aware=True
    )
    embedder = BgeM3Embedder(
        settings.embedding_model,
        revision=settings.embedding_model_revision,
        device=settings.embedding_device,
    )
    service = SearchService(
        client[settings.mongo_database][settings.mongo_collection],
        embedder,
        UnconfiguredPolicyGateway(),
        index_name=settings.mongo_vector_index,
        num_candidates=settings.search_num_candidates,
    )
    rows = []
    latencies: list[float] = []
    try:
        for case in ready:
            start = time.perf_counter()
            try:
                hits = service.search(case["query"], args.top_k, case.get("visa_types"))
            except SearchUnavailableError as exc:
                print_json({**base, "result": f"BLOCKED: {exc}"})
                return EXIT_BLOCKED
            latencies.append((time.perf_counter() - start) * 1000)
            keys = [(h.policy_document_id, h.document_version, h.chunk_index) for h in hits]
            recall, rr = score_case(keys, case.get("gold", []), args.top_k)
            rows.append(
                {
                    "id": case["id"],
                    "language": case["language"],
                    "recall": recall,
                    "rr": rr,
                    "kind": case.get("kind", "answerable"),
                    "hits": keys,
                }
            )
    finally:
        client.close()

    answerable = [r for r in rows if r["kind"] == "answerable"]
    by_lang: dict[str, dict[str, float]] = {}
    for lang in sorted({r["language"] for r in answerable}):
        subset = [r for r in answerable if r["language"] == lang]
        by_lang[lang] = {
            "n": len(subset),
            "recall@k": statistics.mean(r["recall"] for r in subset),
            "mrr@k": statistics.mean(r["rr"] for r in subset),
        }
    report = {
        **base,
        "model": embedder.info().as_dict(),
        "top_k": args.top_k,
        "num_candidates": settings.search_num_candidates,
        "by_language": by_lang,
        "latency_ms": {
            "p50": statistics.median(latencies),
            "p95": sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)],
        },
        "rows": rows,
    }
    write_json(args.output, report)
    print_json({k: v for k, v in report.items() if k != "rows"})
    return EXIT_OK if answerable else EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())

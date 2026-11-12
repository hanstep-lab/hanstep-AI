"""검색 평가: 언어별 Recall@5, MRR@5, 누출 검사, 지연 시간.

정답(gold)은 검증된 한국어 원문을 보고 사람이 작성해야 한다. ``status != "ready"``인
케이스는 blocked로 보고하며, 절대 통과로 보고하지 않는다.

검색은 운영과 같은 경로(Spring 승인 확인 포함)를 거친다. Spring 설정이 없으면 BLOCKED로 끝난다.
리포트에 측정 조건(승인·활성 문서와 청크 수, 모델 revision, top_k, numCandidates, 질문 수)을
남긴다. ``kind``가 unanswerable인 질문은 Recall·MRR에서 빼고 상위 1위 점수와 문서 id만 남긴다.
``kind``가 answerable·unanswerable이 아니면 검색하지 않고 FAILED로 끝난다.

    uv run python scripts/evaluate_rag.py --cases specs/rag/evaluation.json \
        --output artifacts/rag/evaluation.json
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401

from app.core.config import get_settings
from app.integrations.spring_policy_gateway import SpringPolicyGateway
from app.rag.cli import EXIT_BLOCKED, EXIT_FAILED, EXIT_OK, print_json, write_json
from app.rag.embeddings import BgeM3Embedder
from app.rag.evaluation import (
    by_language,
    case_counts,
    corpus_snapshot,
    effective_num_candidates,
    invalid_kinds,
    latency_summary,
    run_cases,
    select_gateway,
)
from app.rag.retrieval import SearchService, SearchUnavailableError


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
    # 모르는 kind를 answerable로 채점하면 Recall이 조용히 내려간다. 검색 전에 멈춘다.
    unknown = invalid_kinds(ready)
    if unknown:
        print_json({**base, "result": "FAILED: unknown kind", "invalid_kinds": unknown})
        return EXIT_FAILED
    if not settings.mongo_configured():
        print("BLOCKED: MONGO_URI is not configured")
        return EXIT_BLOCKED
    # 운영 검색과 같은 승인 확인(Spring AI-R1)을 거친다. 설정이 없으면 평가하지 않는다.
    gateway = select_gateway(settings)
    if gateway is None:
        print_json(
            {
                **base,
                "result": "BLOCKED: SPRING_INTERNAL_API_URL / SPRING_INTERNAL_API_KEY not "
                "configured (search-eligibility approval check is required)",
            }
        )
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
    collection = client[settings.mongo_database][settings.mongo_collection]
    service = SearchService(
        collection,
        embedder,
        gateway,
        index_name=settings.mongo_vector_index,
        num_candidates=settings.search_num_candidates,
    )
    evaluated_at = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        # 첫 질의 지연에 모델 로딩·워밍업이 섞이지 않게 먼저 로드한다.
        embedder.load()
        try:
            corpus = corpus_snapshot(collection, gateway, embedding_model=embedder.model_name)
            rows, unanswerable, latencies = run_cases(service.search, ready, args.top_k)
        except SearchUnavailableError as exc:
            print_json({**base, "result": f"BLOCKED: {exc}"})
            return EXIT_BLOCKED
    finally:
        client.close()
        if isinstance(gateway, SpringPolicyGateway):
            gateway.close()

    report = {
        **base,
        "evaluated_at": evaluated_at,
        "model": embedder.info().as_dict(),
        "top_k": args.top_k,
        "num_candidates": effective_num_candidates(settings.search_num_candidates, args.top_k),
        "corpus": corpus,
        "questions": case_counts(ready),
        "by_language": by_language(rows),
        "latency_ms": latency_summary(latencies),
        "unanswerable": unanswerable,
        "rows": rows,
    }
    write_json(args.output, report)
    print_json({k: v for k, v in report.items() if k != "rows"})
    return EXIT_OK if rows else EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())

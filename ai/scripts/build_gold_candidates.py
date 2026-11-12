"""검색 평가 gold set 라벨링 표를 만든다(질의별 후보 청크와 원문).

읽기 전용이다. MongoDB에서 청크와 임베딩을 읽고 질의만 로컬에서 임베딩한다. 승인·활성 여부와
관계없이 고르므로 승인 전에도 라벨링을 할 수 있다. 다만 벡터 공간이 같아야 비교할 수 있으므로
설정의 임베딩 모델(``EMBEDDING_MODEL``)로 적재된 청크만 쓴다. 후보는 정답이 아니다.
사람이 원문을 읽고 ``specs/rag/evaluation.json``에 gold를 채운다.

    uv run python scripts/build_gold_candidates.py --cases specs/rag/evaluation.json \
        [--manifest specs/rag/sources.json] [--top-k 8] [--output-dir artifacts/rag/labeling]
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401

from app.core.config import get_settings
from app.rag.cli import EXIT_BLOCKED, EXIT_OK, print_json, write_json
from app.rag.embeddings import BgeM3Embedder
from app.rag.labeling import (
    Candidate,
    pending_cases,
    rank_candidates,
    render_markdown,
    superseded_ids,
)

# 표에 필요 없는 필드. 임베딩은 따로 꺼내 쓴다.
_SKIP_FIELDS = {"_id": 0, "created_at": 0, "updated_at": 0}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=Path("specs/rag/sources.json"))
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/rag/labeling"))
    args = parser.parse_args()

    cases = pending_cases(json.loads(args.cases.read_text(encoding="utf-8")).get("cases", []))
    if not cases:
        print_json({"result": "nothing to label", "needs_gold": 0})
        return EXIT_OK
    settings = get_settings()
    if not settings.mongo_configured():
        print("BLOCKED: MONGO_URI is not configured")
        return EXIT_BLOCKED

    from pymongo import MongoClient

    client: MongoClient[dict[str, Any]] = MongoClient(
        settings.mongo_uri.get_secret_value(), serverSelectionTimeoutMS=5000
    )
    try:
        collection = client[settings.mongo_database][settings.mongo_collection]
        # 검색(retrieval.build_pipeline)처럼 다른 모델의 벡터는 섞지 않는다.
        chunks = list(collection.find({"embedding_model": settings.embedding_model}, _SKIP_FIELDS))
    finally:
        client.close()
    if not chunks:
        print(
            f"BLOCKED: no chunks stored for {settings.embedding_model}; "
            "run ingest_rag_documents.py --apply first"
        )
        return EXIT_BLOCKED
    chunk_vectors = [c.pop("embedding") for c in chunks]

    embedder = BgeM3Embedder(
        settings.embedding_model,
        revision=settings.embedding_model_revision,
        device=settings.embedding_device,
        batch_size=settings.embedding_batch_size,
    )
    query_vectors = [embedder.embed_query(c["query"]) for c in cases]
    ranked = rank_candidates(query_vectors, chunk_vectors, args.top_k)
    candidates = [
        [Candidate(rank, score, chunks[i]) for rank, (i, score) in enumerate(row, start=1)]
        for row in ranked
    ]

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    info = embedder.info()
    summary = (
        f"생성 {datetime.now().isoformat(timespec='seconds')} / 청크 {len(chunks)}개 / "
        f"모델 {info.model_name}@{info.revision[:8]} / "
        f"질의 {len(cases)}개 / 후보 상위 {args.top_k}개"
    )
    markdown = render_markdown(
        cases, candidates, chunks, superseded=superseded_ids(manifest), summary=summary
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    md_path = args.output_dir / "gold_candidates.md"
    md_path.write_text(markdown, encoding="utf-8")
    json_path = args.output_dir / "gold_candidates.json"
    write_json(
        json_path,
        [
            {
                "case": case,
                "candidates": [
                    {"rank": p.rank, "score": round(p.score, 4), **p.chunk} for p in picks
                ],
            }
            for case, picks in zip(cases, candidates, strict=True)
        ],
    )
    print_json(
        {
            "needs_gold": len(cases),
            "chunks": len(chunks),
            "markdown": str(md_path),
            "json": str(json_path),
        }
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

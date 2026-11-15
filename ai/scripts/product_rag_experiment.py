"""금융상품 설명서 검색 오프라인 실험(MongoDB·Spring·유료 API 없이, 로컬 BGE-M3만).

PDF를 운영 extractors로 추출해 문서 제목으로 doc id를 매핑하고, 평가 문항의 정답 근거를
확인한 뒤 청크를 한 번 만들어 variant마다 색인·메모리 안 검색을 해 지표를 낸다. 근거를 찾지
못한 문항은 고치지 않고 invalid로 표시해 집계에서 뺀다. 둘 이상의 variant를 돌리면 미리 정한
선택 규칙을 적용해 보고한다. 청크 임베딩은 ``--cache-dir``에 캐시한다.

    uv run python scripts/product_rag_experiment.py \
        --questions specs/rag/product_questions_v1.json \
        --corpus ../../rag-data/products --variants all \
        --revision 5617a9f61b028005a4858fdac845db406aefb181

``--mapping-only``는 모델을 받지 않고 문서 매핑 표와 invalid 문항만 출력한다.
결과: artifacts/rag/product-eval-{variant}-{시각}.json 과 같은 이름의 .md
"""

from __future__ import annotations

import argparse
import hashlib
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import _bootstrap

from app.rag.cli import EXIT_FAILED, EXIT_OK, print_json, write_json
from app.rag.embeddings import MODEL_FILE_PATTERNS, BgeM3Embedder
from app.rag.experiments.product_analysis import CATEGORIES, LABELS_PATH, load_labels
from app.rag.experiments.product_corpus import (
    CorpusError,
    GoldCheck,
    ProductDocument,
    check_gold,
    load_corpus,
    load_questions,
)
from app.rag.experiments.product_eval import (
    VARIANTS,
    EmbeddingCache,
    category_summary,
    question_table,
    render_markdown,
    run_variants,
    select_variant,
)

DEFAULT_OUTPUT_DIR = _bootstrap.AI_ROOT / "artifacts" / "rag"
DEFAULT_CACHE_DIR = DEFAULT_OUTPUT_DIR / "embedding-cache"


def resolved_revision(model_name: str, revision: str) -> str:
    """로컬 캐시에서 실제로 쓴 모델 커밋 해시(스냅샷 폴더 이름)."""
    from huggingface_hub import snapshot_download

    path = snapshot_download(
        model_name,
        revision=revision or None,
        allow_patterns=MODEL_FILE_PATTERNS,
        local_files_only=True,
    )
    return Path(path).name


def print_mapping(documents: list[ProductDocument], checks: list[GoldCheck]) -> None:
    gold_counts: dict[str, int] = {}
    for check in checks:
        if check.doc_id is not None:
            gold_counts[check.doc_id] = gold_counts.get(check.doc_id, 0) + 1
    print("doc id            | 이름 | 종류 | 쪽 | 정답 문항 수 | 파일")
    for d in documents:
        print(
            f"{d.doc_id} | {d.name} | {d.kind} | {d.pages} | {gold_counts.get(d.doc_id, 0)} | "
            f"{', '.join(d.files)}"
        )
    invalid = [c for c in checks if not c.valid]
    print(f"\ninvalid 문항 {len(invalid)}개" + (":" if invalid else ""))
    for check in invalid:
        print(f"- {check.question_id}: {check.reason}")


def labels_for(questions_path: Path, labels_path: Path) -> dict[str, str]:
    """라벨 파일이 이 평가 파일(같은 sha256)에 대해 만든 것일 때만 문항별 분류를 돌려준다."""
    labels = load_labels(labels_path)
    digest = hashlib.sha256(questions_path.read_bytes()).hexdigest()
    if labels.get("questions_sha256") != digest:
        return {}
    return {qid: label["category"] for qid, label in labels["labels"].items()}


def parse_variants(value: str) -> list[str]:
    keys = list(VARIANTS) if value == "all" else [v.strip() for v in value.split(",")]
    unknown = [key for key in keys if key not in VARIANTS]
    if unknown or not keys:
        raise argparse.ArgumentTypeError(
            f"unknown variants {unknown}; choose from {list(VARIANTS)}"
        )
    return keys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--questions", type=Path, required=True, help="평가 문항 JSON 경로")
    parser.add_argument("--corpus", type=Path, required=True, help="상품설명서·약관 PDF 폴더")
    parser.add_argument(
        "--variants", type=parse_variants, default="all", help="'all' 또는 '1,2' 같은 목록"
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--labels", type=Path, default=LABELS_PATH, help="① 실패 분류 라벨")
    parser.add_argument("--model", default="BAAI/bge-m3")
    parser.add_argument("--revision", default="", help="HF 커밋 해시(비우면 기본 브랜치)")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--mapping-only", action="store_true")
    args = parser.parse_args()
    keys: list[str] = args.variants if isinstance(args.variants, list) else list(VARIANTS)

    try:
        version, questions = load_questions(args.questions)
        documents = load_corpus(args.corpus)
        categories = labels_for(args.questions, args.labels)
    except CorpusError as exc:
        print(f"FAILED: {exc}")
        return EXIT_FAILED
    checks = check_gold(questions, documents)
    print_mapping(documents, checks)
    if args.mapping_only:
        return EXIT_OK
    if not any(c.valid for c in checks):
        print("FAILED: no valid questions")
        return EXIT_FAILED

    embedder = BgeM3Embedder(
        args.model, revision=args.revision, device=args.device, batch_size=args.batch_size
    )
    evaluated_at = datetime.now(UTC)
    # 첫 질의 지연에 모델 다운로드·로딩·워밍업이 섞이지 않게 먼저 로드한다.
    start = time.perf_counter()
    embedder.load()
    load_seconds = round(time.perf_counter() - start, 3)
    revision = resolved_revision(args.model, args.revision)
    model = {
        **embedder.info().as_dict(),
        "resolved_revision": revision,
        "load_seconds": load_seconds,
    }
    cache = EmbeddingCache(args.cache_dir, args.model, revision)
    body = run_variants(
        [VARIANTS[key] for key in keys], documents, questions, checks, embedder, cache
    )
    report: dict[str, Any] = {
        "evaluated_at": evaluated_at.isoformat(timespec="seconds"),
        "questions_file": args.questions.as_posix(),
        "questions_version": version,
        "model": model,
        "labels_applied": bool(categories),
        **body,
    }
    report["question_table"] = question_table(report, categories)
    if len(keys) > 1:
        report["selection"] = select_variant(report)
        if keys[0] == "1":
            report["category_summary"] = category_summary(report, categories, CATEGORIES)
            n = report["questions"]["evaluated"]
            hit = round(report["variants"]["1"]["metrics"]["all"][f"recall@{report['top_k']}"] * n)
            report["selection_note"] = (
                f"Recall@{report['top_k']}는 ①이 이미 {hit}/{n}이라 올라갈 여지가 {n - hit}문항"
            )
    stem = f"product-eval-{'-'.join(keys)}-{evaluated_at.strftime('%Y%m%dT%H%M%SZ')}"
    json_path = args.output_dir / f"{stem}.json"
    write_json(json_path, report)
    md_path = json_path.with_suffix(".md")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print_json(
        {
            "model": model,
            "corpus": {k: v for k, v in body["corpus"].items() if k != "detail"},
            "variants": {
                key: {
                    "metrics": result["metrics"],
                    "latency_ms": result["latency_ms"],
                    "index": result["index"],
                }
                for key, result in body["variants"].items()
            },
            "selection": report.get("selection"),
        }
    )
    print(f"\nwrote {json_path}\nwrote {md_path}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

"""금융상품 설명서 검색 오프라인 실험(MongoDB·Spring·유료 API 없이, 로컬 BGE-M3만).

PDF를 운영 extractors로 추출해 문서 제목으로 doc id를 매핑하고, 평가 문항의 정답 근거를
확인한 뒤 variant 설정대로 청킹·임베딩·메모리 안 검색을 해 지표를 낸다. 근거를 찾지 못한
문항은 고치지 않고 invalid로 표시해 집계에서 뺀다.

    uv run python scripts/product_rag_experiment.py \
        --questions specs/rag/product_questions_v1.json \
        --corpus ../../rag-data/products --variant 1

``--mapping-only``는 모델을 받지 않고 문서 매핑 표와 invalid 문항만 출력한다.
결과: artifacts/rag/product-eval-{variant}-{시각}.json 과 같은 이름의 .md
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

import _bootstrap

from app.rag.cli import EXIT_FAILED, EXIT_OK, print_json, write_json
from app.rag.embeddings import MODEL_FILE_PATTERNS, BgeM3Embedder
from app.rag.experiments.product_corpus import (
    CorpusError,
    GoldCheck,
    ProductDocument,
    check_gold,
    load_corpus,
    load_questions,
)
from app.rag.experiments.product_eval import VARIANTS, render_markdown, run_experiment

DEFAULT_OUTPUT_DIR = _bootstrap.AI_ROOT / "artifacts" / "rag"


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--questions", type=Path, required=True, help="평가 문항 JSON 경로")
    parser.add_argument("--corpus", type=Path, required=True, help="상품설명서·약관 PDF 폴더")
    parser.add_argument("--variant", choices=sorted(VARIANTS), default="1")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--model", default="BAAI/bge-m3")
    parser.add_argument("--revision", default="", help="HF 커밋 해시(비우면 기본 브랜치)")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--mapping-only", action="store_true")
    args = parser.parse_args()

    try:
        version, questions = load_questions(args.questions)
        documents = load_corpus(args.corpus)
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

    variant = VARIANTS[args.variant]
    embedder = BgeM3Embedder(
        args.model, revision=args.revision, device=args.device, batch_size=args.batch_size
    )
    evaluated_at = datetime.now(UTC)
    # 첫 질의 지연에 모델 다운로드·로딩·워밍업이 섞이지 않게 먼저 로드한다.
    embedder.load()
    model = {
        **embedder.info().as_dict(),
        "resolved_revision": resolved_revision(args.model, args.revision),
    }
    body = run_experiment(variant, documents, questions, checks, embedder)
    report = {
        "evaluated_at": evaluated_at.isoformat(timespec="seconds"),
        "questions_file": args.questions.as_posix(),
        "questions_version": version,
        "model": model,
        **body,
    }
    stem = f"product-eval-{variant.key}-{evaluated_at.strftime('%Y%m%dT%H%M%SZ')}"
    json_path = args.output_dir / f"{stem}.json"
    write_json(json_path, report)
    md_path = json_path.with_suffix(".md")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print_json({k: v for k, v in report.items() if k not in {"rows", "corpus"}})
    print(f"\nwrote {json_path}\nwrote {md_path}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

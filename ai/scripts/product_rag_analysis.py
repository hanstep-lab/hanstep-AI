"""금융상품 검색 ① 실패 분석과 말뭉치 점검(임베딩·모델 로드 없이, 로컬 토크나이저만).

① 보고서(JSON)와 같은 설정으로 청크를 다시 만들고 ① 보고서의 문서별 청크 수와 같은지 확인한다.
분류 라벨은 사람이 붙인 파일(app/rag/experiments/product_failure_labels_v1.json)에서 읽는다.

    uv run python scripts/product_rag_analysis.py \
        --report artifacts/rag/product-eval-1-<시각>.json \
        --questions specs/rag/product_questions_v1.json \
        --corpus ../../rag-data/products \
        --revision 5617a9f61b028005a4858fdac845db406aefb181

결과: artifacts/rag/product-analysis-{시각}.json 과 같은 이름의 .md
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import _bootstrap

from app.rag.cli import EXIT_FAILED, EXIT_OK, write_json
from app.rag.embeddings import local_tokenizer_counter
from app.rag.experiments.product_analysis import (
    CATEGORIES,
    CATEGORY_NAMES,
    LABELS_PATH,
    category_counts,
    extraction_check,
    failure_cases,
    load_labels,
    near_duplicates,
    security_scan,
    source_list,
)
from app.rag.experiments.product_corpus import CorpusError, load_corpus, load_questions
from app.rag.experiments.product_eval import VARIANTS, build_chunks

DEFAULT_OUTPUT_DIR = _bootstrap.AI_ROOT / "artifacts" / "rag"


def render(report: Mapping[str, Any]) -> str:
    lines = [
        "# 금융상품 검색 ① 실패 분석과 말뭉치 점검",
        "",
        f"- 분석 시각: {report['analyzed_at']}",
        f"- ① 보고서: `{report['source_report']}`",
        f"- 모델 revision: {report['revision']} / 청크 {report['chunks']}개(① 보고서와 일치)",
        f"- 분류 방법: {report['labels_method']}",
        "",
        "## 1-1. 실패 문항",
        "",
        "분류 수: "
        + ", ".join(f"{c} {CATEGORY_NAMES[c]} {n}" for c, n in report["category_counts"].items()),
        "",
    ]
    for case in report["failures"]:
        lines += [
            f"### {case['id']} — {case['category']} {CATEGORY_NAMES[case['category']]}",
            "",
            f"- 질문: {case['question']}",
            f"- 상품명: {'있음' if case['names_product'] else '없음'} / 정답 "
            f"{case['gold_document']} 순위 {case['doc_rank'] or '-'} / 근거 적중 "
            f"{'O' if case['evidence'] else 'X'}",
            f"- 1위 청크({case['top1_document']}, {case['top1_chunk_id']}): {case['top1_excerpt']}",
            f"- 정답 근거 청크({case['evidence_chunk_id']}): {case['evidence_excerpt']}",
            f"- 근거: {case['reason']}",
            "",
        ]
    c_list = [case["id"] for case in report["failures"] if case["category"] == "C"]
    lines += [
        "### C 목록(평가 세트 문제일 수 있음, 문항은 고치지 않음)",
        "",
        f"- {', '.join(c_list) if c_list else '없음'}",
        "",
        "## 1-2. 추출 점검",
        "",
        "| 문서 | 쪽 | 쪽당 최소 | 쪽당 중앙값 | 200자 미만 쪽 | U+FFFD | (cid: | 한글 없는 쪽 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for doc in report["extraction"]:
        lines.append(
            f"| {doc['name']} | {doc['pages']} | {doc['min_chars']} | {doc['median_chars']} | "
            f"{doc['short_pages'] or '-'} | {doc['replacement_chars']} | {doc['cid_patterns']} | "
            f"{doc['no_hangul_pages'] or '-'} |"
        )
    example = report["extraction_example"]
    lines += [
        "",
        f"표 순서 뒤섞임 예시 — {example['chunk']}:",
        "",
        f"> {example['text']}",
        "",
        f"{example['note']}",
        "",
        "## 1-3. 공통 문구 점검",
        "",
        f"- 기준: {report['near_duplicates']['criteria']}",
        f"- 거의 같은 쌍: {report['near_duplicates']['pairs']}개 (앞 100자 같음 "
        f"{report['near_duplicates']['same_prefix_pairs']}, 문장 Jaccard ≥ 0.8 "
        f"{report['near_duplicates']['sentence_overlap_pairs']}, 겹치는 쌍은 한 번)",
    ]
    for pair in report["near_duplicates"]["examples"]:
        lines.append(
            f"  - {pair['a']} ↔ {pair['b']} (앞 100자 같음={pair['same_prefix']}, "
            f"문장 Jaccard={pair['sentence_jaccard']}): {pair['excerpt']}"
        )
    security = report["security"]
    lines += [
        "",
        "## 1-4. 문서 출처·보안 점검(보고만, 필터·수정 없음)",
        "",
        f"모델 revision: {report['revision']}",
        "",
        "| 파일 | doc id | sha256 앞 12 | 쪽 | 사본 |",
        "|---|---|---|---|---|",
    ]
    for source in report["sources"]:
        copy = f"같은 바이트: {', '.join(source['copy_of'])}" if source["copy_of"] else "-"
        lines.append(
            f"| {source['file']} | {source['doc_id']} | {source['sha256_12']} | "
            f"{source['pages']} | {copy} |"
        )
    totals = security["totals"]
    lines += [
        "",
        f"- 13자리 등록번호 형식(6-7): {totals['registration_13']}",
        f"- 10~16자리 연속 숫자: {totals['digits_10_16']}",
        f"- 전화번호: {totals['phone']}",
        f"- URL: {totals['url']}",
        f"- 지시문처럼 보이는 문장(`{security['instruction_words']}`): "
        f"{totals['instruction_like']}",
    ]
    for item in security["instruction_examples"]:
        lines.append(f"  - {item['doc']} ('{item['word']}'): {item['sentence']}")
    lines += [
        "",
        "| 문서 | 등록번호형 | 10~16자리 | 전화 | URL | 지시문형 |",
        "|---|---|---|---|---|---|",
    ]
    for doc in security["per_document"]:
        lines.append(
            f"| {doc['name']} | {doc['registration_13']} | {doc['digits_10_16']} | "
            f"{doc['phone']} | {doc['url']} | {doc['instruction_like']} |"
        )
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", type=Path, required=True, help="① 결과 JSON")
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--labels", type=Path, default=LABELS_PATH)
    parser.add_argument("--model", default="BAAI/bge-m3")
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    source = json.loads(args.report.read_text(encoding="utf-8"))
    if source["model"]["resolved_revision"] != args.revision:
        print("FAILED: --revision differs from the ① report's resolved revision")
        return EXIT_FAILED
    count = local_tokenizer_counter(args.model, args.revision)
    if count is None:
        print("FAILED: tokenizer is not in the local cache")
        return EXIT_FAILED
    try:
        _, questions = load_questions(args.questions)
        documents = load_corpus(args.corpus)
        labels = load_labels(args.labels)
    except CorpusError as exc:
        print(f"FAILED: {exc}")
        return EXIT_FAILED
    chunks = build_chunks(documents, VARIANTS[source["variant"]["key"]], count)
    expected = {d["doc_id"]: d["chunks"] for d in source["corpus"]["detail"]}
    actual: dict[str, int] = {}
    for chunk in chunks:
        actual[chunk.doc_id] = actual.get(chunk.doc_id, 0) + 1
    if actual != expected:
        print("FAILED: rebuilt chunks differ from the ① report")
        return EXIT_FAILED

    quotes = {q.id: q.gold_quote for q in questions}
    cases = failure_cases(source["rows"], chunks, quotes, labels["labels"])
    analyzed_at = datetime.now(UTC)
    report = {
        "analyzed_at": analyzed_at.isoformat(timespec="seconds"),
        "source_report": args.report.name,
        "revision": args.revision,
        "chunks": len(chunks),
        "labels_method": labels.get("method", ""),
        "categories": list(CATEGORIES),
        "category_counts": category_counts(cases),
        "failures": cases,
        "extraction": extraction_check(documents),
        "extraction_example": labels.get("extraction_example"),
        "near_duplicates": near_duplicates(chunks),
        "sources": source_list(documents),
        "security": security_scan(documents),
    }
    stem = f"product-analysis-{analyzed_at.strftime('%Y%m%dT%H%M%SZ')}"
    json_path = args.output_dir / f"{stem}.json"
    write_json(json_path, report)
    md_path = json_path.with_suffix(".md")
    md_path.write_text(render(report), encoding="utf-8")
    print(f"failures {len(cases)}: {report['category_counts']}")
    print(f"wrote {json_path}\nwrote {md_path}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

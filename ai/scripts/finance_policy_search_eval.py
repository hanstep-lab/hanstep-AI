"""금융 정책 RAG(#88) 2단계 검색 비교: 청크 ①②③ × 한국어·영어 검색어(LLM 호출 없음, 로컬 BGE-M3).

- ① fixed: chunk_blocks 기본값(600/900/80) + BGE-M3 dense 코사인
- ② unit: 조문·소제목 단위 + dense. 30토큰 미만은 다음 단위에 붙이고,
  900 초과는 문단 경계에서 500 이하로 나눈다.
- ③ unit + BM25: ②와 같은 청크, dense 20 + BM25 20을 RRF(k=60)로 합침(#47 ③과 같은 설정)
- ④ ③ + 긴 조문을 나눈 조각마다 조 머리(법령명·조 번호·조 제목)를 붙임. **측정 뒤(2단계 결과를 보고)
  추가한 후보**다. ③과만 비교한다(ko 원 문항 16개 기준, 1문항 이하 차이는 동점).
핵심 사실 포함@3·@5·@8도 낸다(도구가 돌려줄 청크 수 k를 정하는 근거).

현행 조문만 색인한다(시행 예정 판 제외). 문항은 answerable 32(original 16 + user 16).
검색어: ko = queryKo(original·user 같음), en = original은 queryEn, user는 사용자 질문 원문.

지표(상위 5 청크, 결정 2026-11-17):
- Recall@5: 핵심 사실 인용이 하나라도 들어 있는 청크가 상위 5에 있음
  (근거 청크 = 같은 문서이고 인용을 공백 무시하고 포함).
- 핵심 사실 포함@5: 그 문항의 핵심 사실 인용이 모두 상위 5 청크 안에 있음.
- MRR@5: 첫 근거 청크 순위의 역수.
고르는 순서: Recall@5 → 핵심 사실 포함@5 → MRR@5 → 단순(① > ② > ③). 각 단계에서 최고와 1문항
(MRR은 1/n) 이하 차이는 동점. 챗봇은 한국어 검색어를 쓰므로 선택은 ko로 하고 en은 따로 보고한다.

    HF_HUB_OFFLINE=1 uv run python scripts/finance_policy_search_eval.py \\
        --corpus ../../rag-data/finance-policy --revision 5617a9f61b028005a4858fdac845db406aefb181
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import _bootstrap

from app.finance_policy.chunks import PolicyChunk, article_heads, fixed_chunks, unit_chunks
from app.finance_policy.corpus import load_corpus, match_key

AI_ROOT = _bootstrap.AI_ROOT
QUESTIONS_PATH = AI_ROOT / "specs" / "rag" / "finance_policy_questions_v1.json"
DEFAULT_OUTPUT_DIR = AI_ROOT / "artifacts" / "rag"
DEFAULT_CACHE_DIR = DEFAULT_OUTPUT_DIR / "embedding-cache"
TOP_K = 5
COVERAGE_KS = (3, 5, 8)
METHODS = ("1", "2", "3", "4")
# 2단계 미리 정한 비교(①②③)와 측정 뒤 추가한 비교(③④).
FIRST_ROUND = ("1", "2", "3")
SECOND_ROUND = ("3", "4")
LANGS = ("ko", "en")
DESCRIPTIONS = {
    "1": "① 고정 길이(600/900/80) + dense",
    "2": "② 조문·소제목 단위(규칙 적용) + dense",
    "3": "③ ② + BM25(RRF k=60, 각 상위 20)",
    "4": "④ ③ + 나눈 조문 조각마다 조 머리(측정 뒤 추가한 후보)",
}


def query_text(question: Mapping[str, Any], lang: str) -> str:
    if lang == "ko":
        return str(question["queryKo"])
    return str(question["text"] if question.get("variant") == "user" else question["queryEn"])


def relevant(chunk: PolicyChunk, fact: Mapping[str, Any]) -> bool:
    return chunk.doc == fact["doc"] and match_key(fact["quote"]) in match_key(chunk.text)


def score(
    question: Mapping[str, Any], ranked: Sequence[PolicyChunk], corpus_chunks: Sequence[PolicyChunk]
) -> dict[str, Any]:
    """ranked는 상위 max(COVERAGE_KS)개. 주 지표는 상위 TOP_K로 낸다."""
    facts = question["keyFacts"]
    top = ranked[:TOP_K]
    ranks = [i for i, c in enumerate(top, start=1) if any(relevant(c, f) for f in facts)]
    covered = [any(relevant(c, f) for c in top) for f in facts]
    in_corpus = [any(relevant(c, f) for c in corpus_chunks) for f in facts]
    return {
        "hit": bool(ranks),
        "rr": 1.0 / ranks[0] if ranks else 0.0,
        "rank": ranks[0] if ranks else None,
        "allFacts": all(covered),
        "factsCovered": covered,
        "factsInCorpus": in_corpus,
        "allFactsAt": {
            str(k): all(any(relevant(c, f) for c in ranked[:k]) for f in facts) for k in COVERAGE_KS
        },
        "top": [{"chunkId": c.chunk_id, "doc": c.doc} for c in top],
    }


def aggregate(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    if not n:
        return {"n": 0}
    return {
        "n": n,
        "recall": sum(r["hit"] for r in rows),
        "allFacts": sum(r["allFacts"] for r in rows),
        "mrr": statistics.mean(r["rr"] for r in rows),
        "allFactsAt": {str(k): sum(r["allFactsAt"][str(k)] for r in rows) for k in COVERAGE_KS},
    }


def select(
    summary: Mapping[str, Mapping[str, Any]], methods: Sequence[str] = FIRST_ROUND
) -> dict[str, Any]:
    """Recall → 핵심 사실 포함 → MRR → 단순(methods 순서). 최고와 1문항(MRR은 1/n) 이하는 동점."""
    alive = list(methods)
    steps = []
    n = next(iter(summary.values()))["n"]
    for metric, tol in (("recall", 1), ("allFacts", 1), ("mrr", 1 / n)):
        best = max(summary[m][metric] for m in alive)
        kept = [m for m in alive if best - summary[m][metric] <= tol + 1e-9]
        steps.append({"metric": metric, "best": best, "kept": kept})
        alive = kept
    chosen = alive[0]  # methods 순서가 단순한 순서
    return {"steps": steps, "chosen": chosen, "distinguished": len(steps[0]["kept"]) < len(methods)}


def build(chunks: Sequence[PolicyChunk], method: str, embedder: Any, cache: Any) -> Any:
    from app.rag.experiments.product_eval import (
        BM25Index,
        CorpusChunk,
        DenseRetriever,
        HybridRetriever,
    )

    wrapped = [
        CorpusChunk(c.chunk_id, c.doc, i, c.page, None, c.text, c.text)
        for i, c in enumerate(chunks)
    ]
    vectors, _ = cache.get_or_compute(wrapped, "none", embedder.embed_documents)
    dense = DenseRetriever(embedder, vectors)
    if method not in ("3", "4"):
        return dense
    return HybridRetriever(dense, BM25Index([c.text for c in chunks], 1.5, 0.75), 60, 20)


def _cell(result: Mapping[str, Any]) -> str:
    return f"{result['rank'] or '-'}/{'O' if result['allFacts'] else 'X'}"


def render(report: Mapping[str, Any]) -> str:
    out = [
        "# 금융 정책 검색 비교 (2단계)",
        "",
        f"실행 {report['startedAt']}, 모델 {report['model']} @ {report['revision']}",
        "LLM 호출 없음.",
        "청크 수: " + ", ".join(f"{m} {report['chunks'][m]}" for m in METHODS),
        "",
        "| 방식 | 언어 | 묶음 | n | Recall@5 | 핵심 사실 포함@5 | MRR@5 | 포함@3 | 포함@8 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for m in METHODS:
        for lang in LANGS:
            for group, agg in report["summary"][m][lang].items():
                out.append(
                    f"| {m} | {lang} | {group} | {agg['n']} | {agg['recall']}/{agg['n']} | "
                    f"{agg['allFacts']}/{agg['n']} | {agg['mrr']:.3f} | "
                    f"{agg['allFactsAt']['3']}/{agg['n']} | {agg['allFactsAt']['8']}/{agg['n']} |"
                )
    for lang, sel in report["selection"].items():
        out += [
            "",
            f"선택({lang}): "
            + " → ".join(f"{s['metric']} 남음 {s['kept']}" for s in sel["steps"])
            + f" ⇒ **{sel['chosen']}**"
            + ("" if sel["distinguished"] else " (Recall@5로 구분 안 됨)"),
        ]
    out += [
        "",
        "| 문항 | variant | 언어 | " + " | ".join(f"{m} 순위/전부" for m in METHODS) + " |",
        "|---|---|---|" + "---|" * len(METHODS),
    ]
    for row in report["rows"]:
        cells = [_cell(row["methods"][m]) for m in METHODS]
        out.append(
            f"| {row['id']} | {row['variant']} | {row['lang']} | " + " | ".join(cells) + " |"
        )
    return "\n".join(out) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--corpus", type=Path, required=True, help="금융 정책 문서 폴더")
    parser.add_argument("--questions", type=Path, default=QUESTIONS_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--model", default="BAAI/bge-m3")
    parser.add_argument("--revision", required=True)
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args(argv)

    from app.rag.embeddings import BgeM3Embedder
    from app.rag.experiments.product_eval import EmbeddingCache

    corpus = load_corpus(args.corpus)
    qset = json.loads(args.questions.read_text(encoding="utf-8"))
    questions = [q for q in qset["questions"] if q["category"] == "answerable"]
    embedder = BgeM3Embedder(args.model, revision=args.revision, device=args.device)
    embedder.load()
    cache = EmbeddingCache(args.cache_dir, args.model, args.revision)
    chunks = {
        "1": fixed_chunks(corpus.units, embedder.count_tokens),
        "2": unit_chunks(corpus.units, embedder.count_tokens),
    }
    chunks["3"] = chunks["2"]
    titles = {d["key"]: d["title"] for d in corpus.manifest["documents"]}
    heads = article_heads(corpus.units, titles)
    chunks["4"] = unit_chunks(corpus.units, embedder.count_tokens, heads)
    retrievers = {m: build(chunks[m], m, embedder, cache) for m in METHODS}
    for r in retrievers.values():
        r.search("해외송금 한도", TOP_K)  # 워밍업
    started = datetime.now(UTC)
    rows = []
    for q in questions:
        for lang in LANGS:
            text = query_text(q, lang)
            per = {}
            for m in METHODS:
                ranked = [chunks[m][i] for i, _ in retrievers[m].search(text, max(COVERAGE_KS))]
                per[m] = score(q, ranked, chunks[m])
            rows.append({"id": q["id"], "variant": q.get("variant", "original"), "lang": lang,
                         "query": text, "methods": per})  # fmt: skip
    summary: dict[str, Any] = {}
    for m in METHODS:
        summary[m] = {}
        for lang in LANGS:
            mine = [r["methods"][m] for r in rows if r["lang"] == lang]
            groups = {"all": mine}
            for variant in ("original", "user"):
                groups[variant] = [r["methods"][m] for r in rows
                                   if r["lang"] == lang and r["variant"] == variant]  # fmt: skip
            summary[m][lang] = {g: aggregate(v) for g, v in groups.items()}
    selection = {lang: select({m: summary[m][lang]["all"] for m in METHODS}) for lang in LANGS}
    # ③ vs ④(측정 뒤 추가): ko는 original·user가 같은 검색어라 원 문항 16개로 비교한다.
    selection["ko ③④ (원 문항 16)"] = select(
        {m: summary[m]["ko"]["original"] for m in SECOND_ROUND}, SECOND_ROUND
    )
    report = {
        "startedAt": started.isoformat(timespec="seconds"),
        "model": args.model,
        "revision": args.revision,
        "units": len(corpus.units),
        "scheduledExcluded": [u.unit_id for u in corpus.scheduled],
        "chunks": {m: len(chunks[m]) for m in METHODS},
        "descriptions": DESCRIPTIONS,
        "summary": summary,
        "selection": selection,
        "rows": rows,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"finance-policy-search-{started.strftime('%Y%m%dT%H%M%SZ')}"
    (args.output_dir / f"{stem}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.output_dir / f"{stem}.md").write_text(render(report), encoding="utf-8")
    print(json.dumps({"chunks": report["chunks"], "selection": selection}, ensure_ascii=False))
    for m in METHODS:
        for lang in LANGS:
            a = summary[m][lang]["all"]
            line = f"{m} {lang} recall {a['recall']}/{a['n']} allFacts {a['allFacts']}"
            print(f"{line} mrr {a['mrr']:.3f} allFactsAt {a['allFactsAt']}")
    print(f"wrote {args.output_dir / stem}.json/.md")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    raise SystemExit(main())

"""금융상품 설명서 검색 오프라인 실험: variant 설정, 메모리 안 검색, 문서·근거 지표, 보고서.

variant는 ``VariantConfig``로 받는다. 청크를 임베딩할 텍스트(``chunk_prefix``)와 검색 방식
(``retrieval``)이 갈아 끼울 지점이다. 지금은 ①(chunk_blocks 기본 설정 + BGE-M3 dense 코사인)만
구현돼 있다. 상품명·섹션 제목 prefix나 BM25 + RRF는 이 두 값에 선택지를 더해 끼운다.

지표 정의(모두 상위 ``top_k`` 청크 기준):
- 문서 순위: 상위 청크를 순서대로 보며 같은 문서의 청크는 첫 순위만 남긴 문서 목록의 순위.
- Recall@1: 1위 청크가 정답 문서. Recall@k: 정답 문서가 상위 k 청크 안에 있음.
- MRR@k: 정답 문서 순위의 역수(없으면 0).
- 근거 적중@k: 정답 문서의 상위 k 청크 중 하나가 goldQuote를 포함(공백·줄바꿈 무시).
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol

import numpy as np

from app.rag.chunking import ChunkDraft, ChunkerConfig, TokenCounter, chunk_blocks
from app.rag.embeddings import Embedder
from app.rag.evaluation import latency_summary
from app.rag.experiments.product_corpus import (
    GoldCheck,
    ProductDocument,
    Question,
    match_key,
)

# 청크를 임베딩·검색할 때 본문 앞에 붙이는 문맥. "none"은 청크 원문 그대로다.
ChunkPrefix = Literal["none"]
# 검색 방식. "dense"는 BGE-M3 dense 벡터의 코사인 유사도다.
RetrievalMode = Literal["dense"]


@dataclass(frozen=True)
class VariantConfig:
    """실험 variant 하나의 설정."""

    key: str
    description: str
    chunker: ChunkerConfig = field(default_factory=ChunkerConfig)
    chunk_prefix: ChunkPrefix = "none"
    retrieval: RetrievalMode = "dense"
    top_k: int = 5

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


VARIANTS: dict[str, VariantConfig] = {
    "1": VariantConfig(
        key="1",
        description="chunk_blocks 기본 설정(600/900/80) + BGE-M3 dense, 메모리 안 코사인 top-5",
    ),
}


@dataclass(frozen=True)
class CorpusChunk:
    """검색 대상 청크. ``text``는 원문(근거 판정용), ``embed_text``는 검색에 넣는 텍스트."""

    chunk_id: str
    doc_id: str
    chunk_index: int
    page_number: int | None
    section_title: str | None
    text: str
    embed_text: str


def embed_text(draft: ChunkDraft, document: ProductDocument, variant: VariantConfig) -> str:
    """variant의 ``chunk_prefix``에 따라 청크를 검색용 텍스트로 만든다."""
    if variant.chunk_prefix == "none":
        return draft.text
    raise NotImplementedError(f"chunk_prefix {variant.chunk_prefix!r}")


def build_chunks(
    documents: Sequence[ProductDocument], variant: VariantConfig, count: TokenCounter
) -> list[CorpusChunk]:
    chunks: list[CorpusChunk] = []
    for document in documents:
        for draft in chunk_blocks(document.blocks, count, variant.chunker):
            chunks.append(
                CorpusChunk(
                    chunk_id=f"{document.doc_id}#{draft.chunk_index}",
                    doc_id=document.doc_id,
                    chunk_index=draft.chunk_index,
                    page_number=draft.page_number,
                    section_title=draft.section_title,
                    text=draft.text,
                    embed_text=embed_text(draft, document, variant),
                )
            )
    return chunks


class Retriever(Protocol):
    """질의 하나에 (청크 위치, 점수)를 점수 내림차순으로 돌려주는 검색기."""

    def search(self, query: str, k: int) -> list[tuple[int, float]]: ...


class DenseRetriever:
    """청크 임베딩을 메모리에 두고 질의와의 코사인 유사도로 순위를 매긴다."""

    def __init__(self, embedder: Embedder, chunks: Sequence[CorpusChunk]) -> None:
        self._embedder = embedder
        matrix = np.asarray(embedder.embed_documents([c.embed_text for c in chunks]), np.float64)
        self._matrix = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)

    def search(self, query: str, k: int) -> list[tuple[int, float]]:
        vector = np.asarray(self._embedder.embed_query(query), np.float64)
        scores = self._matrix @ (vector / np.linalg.norm(vector))
        # 동점은 청크 순서로 정해 결과를 결정적으로 만든다.
        order = np.argsort(-scores, kind="stable")[:k]
        return [(int(i), float(scores[i])) for i in order]


def build_retriever(
    variant: VariantConfig, embedder: Embedder, chunks: Sequence[CorpusChunk]
) -> Retriever:
    if variant.retrieval == "dense":
        return DenseRetriever(embedder, chunks)
    raise NotImplementedError(f"retrieval {variant.retrieval!r}")


def score_question(gold_doc_id: str, gold_quote: str, top: Sequence[CorpusChunk]) -> dict[str, Any]:
    """상위 청크 목록 ``top``으로 문항 하나를 채점한다(정의는 모듈 docstring)."""
    doc_order = list(dict.fromkeys(chunk.doc_id for chunk in top))
    rank = doc_order.index(gold_doc_id) + 1 if gold_doc_id in doc_order else None
    quote = match_key(gold_quote)
    evidence = [
        chunk.chunk_id
        for chunk in top
        if chunk.doc_id == gold_doc_id and quote in match_key(chunk.text)
    ]
    return {
        "doc_rank": rank,
        "recall@1": 1.0 if top and top[0].doc_id == gold_doc_id else 0.0,
        "recall@k": 1.0 if rank is not None else 0.0,
        "rr": 1.0 / rank if rank is not None else 0.0,
        "evidence@k": 1.0 if evidence else 0.0,
        "evidence_chunks": evidence,
    }


def aggregate(rows: Sequence[Mapping[str, Any]], k: int) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    return {
        "n": len(rows),
        "recall@1": statistics.mean(r["recall@1"] for r in rows),
        f"recall@{k}": statistics.mean(r["recall@k"] for r in rows),
        f"mrr@{k}": statistics.mean(r["rr"] for r in rows),
        f"evidence@{k}": statistics.mean(r["evidence@k"] for r in rows),
    }


def run_experiment(
    variant: VariantConfig,
    documents: Sequence[ProductDocument],
    questions: Sequence[Question],
    checks: Sequence[GoldCheck],
    embedder: Embedder,
    *,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    """말뭉치를 청킹·색인하고 유효한 문항만 검색해 보고서 본문을 만든다.

    모델 로딩·워밍업은 호출 전에 끝내야 첫 질의 지연에 섞이지 않는다.
    """
    k = variant.top_k
    check_by_id = {c.question_id: c for c in checks}
    names = {d.doc_id: d.name for d in documents}
    chunks = build_chunks(documents, variant, embedder.count_tokens)
    start = clock()
    retriever = build_retriever(variant, embedder, chunks)
    index_seconds = clock() - start

    rows: list[dict[str, Any]] = []
    latencies: list[float] = []
    for question in questions:
        check = check_by_id[question.id]
        if not check.valid or check.doc_id is None:
            continue
        start = clock()
        ranked = retriever.search(question.question, k)
        latencies.append((clock() - start) * 1000)
        top = [chunks[i] for i, _ in ranked]
        score = score_question(check.doc_id, question.gold_quote, top)
        evidence = set(score["evidence_chunks"])
        rows.append(
            {
                "id": question.id,
                "question": question.question,
                "names_product": question.names_product,
                "gold_doc_id": check.doc_id,
                "gold_document": names[check.doc_id],
                "gold_page": question.page,
                **{key: value for key, value in score.items() if key != "evidence_chunks"},
                "hits": [
                    {
                        "rank": rank,
                        "chunk_id": chunk.chunk_id,
                        "doc_id": chunk.doc_id,
                        "document": names[chunk.doc_id],
                        "page": chunk.page_number,
                        "score": round(value, 6),
                        "gold_doc": chunk.doc_id == check.doc_id,
                        "quote_hit": chunk.chunk_id in evidence,
                    }
                    for rank, (chunk, (_, value)) in enumerate(zip(top, ranked, strict=True), 1)
                ],
            }
        )

    chunk_counts: dict[str, int] = {}
    for chunk in chunks:
        chunk_counts[chunk.doc_id] = chunk_counts.get(chunk.doc_id, 0) + 1
    invalid = [
        {"id": c.question_id, "doc_id": c.doc_id, "reason": c.reason} for c in checks if not c.valid
    ]
    return {
        "variant": variant.as_dict(),
        "top_k": k,
        "corpus": {
            "files": sum(len(d.files) for d in documents),
            "documents": len(documents),
            "chunks": len(chunks),
            "detail": [{**d.summary(), "chunks": chunk_counts.get(d.doc_id, 0)} for d in documents],
        },
        "questions": {
            "total": len(questions),
            "evaluated": len(rows),
            "invalid": invalid,
        },
        "metrics": {
            "all": aggregate(rows, k),
            "namesProduct=true": aggregate([r for r in rows if r["names_product"]], k),
            "namesProduct=false": aggregate([r for r in rows if not r["names_product"]], k),
        },
        "latency_ms": latency_summary(latencies),
        "index_build_seconds": round(index_seconds, 3),
        "rows": rows,
    }


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def render_markdown(report: Mapping[str, Any]) -> str:
    """JSON 보고서를 사람이 읽는 Markdown으로 만든다."""
    k = report["top_k"]
    variant = report["variant"]
    model = report["model"]
    corpus = report["corpus"]
    questions = report["questions"]
    latency = report["latency_ms"]
    lines = [
        f"# 금융상품 설명서 검색 평가 — variant {variant['key']}",
        "",
        "## 측정 조건",
        "",
        f"- 평가 시각: {report['evaluated_at']}",
        f"- 평가 파일: `{report['questions_file']}` (version {report['questions_version']})",
        f"- variant: {variant['description']}",
        f"- 청커: {variant['chunker']}, prefix={variant['chunk_prefix']}, "
        f"검색={variant['retrieval']}, top_k={k}",
        f"- 임베딩: {model['model_name']} revision={model['resolved_revision']} "
        f"(요청 {model['revision']}), device={model['device']}, "
        f"sentence-transformers {model['sentence_transformers']}, torch {model['torch']}",
        f"- 말뭉치: PDF {corpus['files']}개 → 문서 {corpus['documents']}개, "
        f"청크 {corpus['chunks']}개",
        f"- 문항: 전체 {questions['total']}, 평가 {questions['evaluated']}, "
        f"invalid {len(questions['invalid'])}",
        f"- 검색 지연(질의 임베딩 + 코사인): 중앙값 {_fmt(latency['p50'])} ms, "
        f"p95 {_fmt(latency['p95'])} ms / 색인 {report['index_build_seconds']} s",
        "",
        "## 지표",
        "",
        f"| 집단 | n | Recall@1 | Recall@{k} | MRR@{k} | 근거 적중@{k} |",
        "|---|---|---|---|---|---|",
    ]
    for group, metrics in report["metrics"].items():
        lines.append(
            f"| {group} | {metrics['n']} | {_fmt(metrics.get('recall@1'))} | "
            f"{_fmt(metrics.get(f'recall@{k}'))} | {_fmt(metrics.get(f'mrr@{k}'))} | "
            f"{_fmt(metrics.get(f'evidence@{k}'))} |"
        )
    lines += ["", "## invalid 문항(집계 제외)", ""]
    if questions["invalid"]:
        lines += [f"- {item['id']}: {item['reason']}" for item in questions["invalid"]]
    else:
        lines.append("- 없음")
    lines += [
        "",
        "## 문서",
        "",
        "| doc id | 이름 | 종류 | 파일 | 쪽 | 청크 |",
        "|---|---|---|---|---|---|",
    ]
    for doc in corpus["detail"]:
        lines.append(
            f"| {doc['doc_id']} | {doc['name']} | {doc['kind']} | {', '.join(doc['files'])} | "
            f"{doc['pages']} | {doc['chunks']} |"
        )
    lines += ["", f"## 문항별 상위 {k}", ""]
    for row in report["rows"]:
        mark = "✅" if row["recall@1"] else ("🟡" if row["recall@k"] else "❌")
        lines += [
            f"### {mark} {row['id']} (namesProduct={str(row['names_product']).lower()})",
            "",
            f"- 질문: {row['question']}",
            f"- 정답: {row['gold_document']} p.{row['gold_page']} / 문서 순위 "
            f"{_fmt(row['doc_rank'])} / 근거 적중 {'O' if row['evidence@k'] else 'X'}",
            "",
            "| 순위 | 문서 | 쪽 | 점수 | 정답 문서 | 근거 |",
            "|---|---|---|---|---|---|",
        ]
        for hit in row["hits"]:
            lines.append(
                f"| {hit['rank']} | {hit['document']} | {_fmt(hit['page'])} | "
                f"{hit['score']:.4f} | {'O' if hit['gold_doc'] else ''} | "
                f"{'O' if hit['quote_hit'] else ''} |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"

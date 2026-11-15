"""금융상품 설명서 검색 오프라인 실험: variant 설정, 메모리 안 검색, 문서·근거 지표, 비교·선택.

variant는 ``VariantConfig``로 받는다. 청크를 임베딩할 텍스트(``chunk_prefix``)와 검색 방식
(``retrieval``)이 갈아 끼울 지점이다. 청크 목록은 한 번 만들어 모든 variant가 같이 쓰고,
근거 적중은 언제나 prefix 없는 청크 본문으로 판정한다.

- ①: chunk_blocks 기본 설정(600/900/80) + BGE-M3 dense 코사인.
- ②: ①과 같은 청크, 임베딩 입력에만 "[상품명] {문서 이름} / [섹션] {섹션 제목}\\n"을 붙인다.
  섹션 제목은 chunk_blocks가 준 ``section_title``만 쓰고, 없으면 상품명만 붙인다. 질의는 그대로.
- ③: ① dense(prefix 없음)와 같은 청크 본문 BM25(k1=1.5, b=0.75)를 각각 상위 20개씩 RRF(k=60)로
  합친다. BM25 토큰은 공백으로 나눈 토큰(소문자) + 공백을 뺀 한글 글자 2-gram.

지표 정의(모두 상위 ``top_k`` 청크 기준):
- 문서 순위: 상위 청크를 순서대로 보며 같은 문서의 청크는 첫 순위만 남긴 문서 목록의 순위.
- Recall@1: 1위 청크가 정답 문서. Recall@k: 정답 문서가 상위 k 청크 안에 있음.
- MRR@k: 정답 문서 순위의 역수(없으면 0).
- 근거 적중@k: 정답 문서의 상위 k 청크 중 하나가 goldQuote를 포함(공백·줄바꿈 무시).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
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

# 청크를 임베딩할 때 본문 앞에 붙이는 문맥. "none"은 청크 원문 그대로다.
ChunkPrefix = Literal["none", "title_section"]
# 검색 방식. "dense"는 BGE-M3 dense 벡터의 코사인 유사도, "dense_bm25_rrf"는 dense와 BM25의 RRF.
RetrievalMode = Literal["dense", "dense_bm25_rrf"]

WARMUP_QUERY = "정기예금 가입 조건"


@dataclass(frozen=True)
class VariantConfig:
    """실험 variant 하나의 설정."""

    key: str
    description: str
    chunker: ChunkerConfig = field(default_factory=ChunkerConfig)
    chunk_prefix: ChunkPrefix = "none"
    retrieval: RetrievalMode = "dense"
    top_k: int = 5
    # dense_bm25_rrf에서만 쓴다.
    bm25_k1: float = 1.5
    bm25_b: float = 0.75
    rrf_k: int = 60
    rrf_depth: int = 20

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# 선언 순서가 '단순한 순서'다(선택 규칙의 마지막 동점 처리).
VARIANTS: dict[str, VariantConfig] = {
    "1": VariantConfig(
        key="1",
        description="chunk_blocks 기본 설정(600/900/80) + BGE-M3 dense, 메모리 안 코사인 top-5",
    ),
    "2": VariantConfig(
        key="2",
        description="①과 같은 청크 + 임베딩 입력에만 '[상품명] … / [섹션] …' prefix, dense top-5",
        chunk_prefix="title_section",
    ),
    "3": VariantConfig(
        key="3",
        description="① dense + 청크 본문 BM25(k1=1.5, b=0.75), 각 상위 20개 RRF(k=60) top-5",
        retrieval="dense_bm25_rrf",
    ),
}


@dataclass(frozen=True)
class CorpusChunk:
    """검색 대상 청크. ``text``는 원문(근거 판정용), ``embed_text``는 임베딩에 넣는 텍스트."""

    chunk_id: str
    doc_id: str
    chunk_index: int
    page_number: int | None
    section_title: str | None
    text: str
    embed_text: str


def embedding_input(
    text: str, section_title: str | None, doc_name: str, prefix: ChunkPrefix
) -> str:
    """``prefix`` 방식에 따라 청크 본문을 임베딩 입력으로 만든다."""
    if prefix == "none":
        return text
    if prefix == "title_section":
        head = f"[상품명] {doc_name}"
        if section_title:
            head += f" / [섹션] {section_title}"
        return f"{head}\n{text}"
    raise NotImplementedError(f"chunk_prefix {prefix!r}")


def embed_text(draft: ChunkDraft, document: ProductDocument, variant: VariantConfig) -> str:
    return embedding_input(draft.text, draft.section_title, document.name, variant.chunk_prefix)


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


def with_prefix(
    chunks: Sequence[CorpusChunk], documents: Sequence[ProductDocument], prefix: ChunkPrefix
) -> list[CorpusChunk]:
    """같은 청크(본문·분할 그대로)의 임베딩 입력만 ``prefix`` 방식으로 바꾼다."""
    names = {d.doc_id: d.name for d in documents}
    return [
        replace(c, embed_text=embedding_input(c.text, c.section_title, names[c.doc_id], prefix))
        for c in chunks
    ]


def texts_sha256(texts: Sequence[str]) -> str:
    return hashlib.sha256(json.dumps(list(texts), ensure_ascii=False).encode()).hexdigest()


class EmbeddingCache:
    """청크 임베딩 캐시. 키 = 모델 revision + 청크 본문 해시 + prefix 방식.

    같은 키에 임베딩 입력 해시가 다르면(prefix 형식을 바꾼 경우) 캐시를 쓰지 않고 다시 계산한다.
    ``directory``가 None이면 캐시하지 않는다.
    """

    def __init__(self, directory: Path | None, model_name: str, revision: str) -> None:
        self.directory = directory
        self.model_name = model_name
        self.revision = revision

    def key(self, body_sha256: str, prefix: str) -> str:
        model = re.sub(r"[^A-Za-z0-9.-]+", "_", self.model_name)
        return f"{model}-{self.revision}-{body_sha256[:16]}-{prefix}"

    def get_or_compute(
        self,
        chunks: Sequence[CorpusChunk],
        prefix: str,
        compute: Callable[[list[str]], list[list[float]]],
        clock: Callable[[], float] = time.perf_counter,
    ) -> tuple[np.ndarray, dict[str, Any]]:
        body = texts_sha256([c.text for c in chunks])
        inputs = [c.embed_text for c in chunks]
        input_hash = texts_sha256(inputs)
        key = self.key(body, prefix)
        info: dict[str, Any] = {"key": key, "embedding_input_sha256": input_hash}
        if self.directory is not None:
            vectors_path = self.directory / f"{key}.npy"
            meta_path = self.directory / f"{key}.json"
            if vectors_path.exists() and meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                vectors = np.load(vectors_path)
                if meta.get("embedding_input_sha256") == input_hash and len(vectors) == len(inputs):
                    return vectors, {**info, "cache": "hit", **_timing(meta)}
        start = clock()
        vectors = np.asarray(compute(inputs), dtype=np.float32)
        seconds = round(clock() - start, 3)
        meta = {
            "model_name": self.model_name,
            "revision": self.revision,
            "prefix": prefix,
            "chunk_text_sha256": body,
            "embedding_input_sha256": input_hash,
            "count": len(inputs),
            "embedding_seconds": seconds,
        }
        if self.directory is not None:
            self.directory.mkdir(parents=True, exist_ok=True)
            np.save(self.directory / f"{key}.npy", vectors)
            (self.directory / f"{key}.json").write_text(json.dumps(meta, indent=2), "utf-8")
        return vectors, {**info, "cache": "miss", **_timing(meta)}


def _timing(meta: Mapping[str, Any]) -> dict[str, Any]:
    return {"embedding_seconds": meta.get("embedding_seconds")}


class Retriever(Protocol):
    """질의 하나에 (청크 위치, 점수)를 점수 내림차순으로 돌려주는 검색기."""

    def search(self, query: str, k: int) -> list[tuple[int, float]]: ...


class DenseRetriever:
    """청크 임베딩을 메모리에 두고 질의와의 코사인 유사도로 순위를 매긴다."""

    def __init__(self, embedder: Embedder, vectors: np.ndarray) -> None:
        self._embedder = embedder
        matrix = np.asarray(vectors, np.float64)
        self._matrix = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)

    def search(self, query: str, k: int) -> list[tuple[int, float]]:
        vector = np.asarray(self._embedder.embed_query(query), np.float64)
        scores = self._matrix @ (vector / np.linalg.norm(vector))
        # 동점은 청크 순서로 정해 결과를 결정적으로 만든다.
        order = np.argsort(-scores, kind="stable")[:k]
        return [(int(i), float(scores[i])) for i in order]


def _is_hangul(char: str) -> bool:
    return "가" <= char <= "힣"


def bm25_tokens(text: str) -> list[str]:
    """공백으로 나눈 토큰(영문 소문자) + 공백을 뺀 한글 글자 2-gram("2:" 접두로 구분)."""
    words = text.lower().split()
    compact = "".join(text.split())
    bigrams = [
        f"2:{compact[i : i + 2]}"
        for i in range(len(compact) - 1)
        if _is_hangul(compact[i]) and _is_hangul(compact[i + 1])
    ]
    return words + bigrams


class BM25Index:
    """Okapi BM25(idf = ln(1 + (N - df + 0.5) / (df + 0.5))). 질의 토큰은 중복 없이 센다."""

    def __init__(self, texts: Sequence[str], k1: float, b: float) -> None:
        self._k1 = k1
        self._b = b
        self._tfs = [Counter(bm25_tokens(text)) for text in texts]
        self._lengths = [sum(tf.values()) for tf in self._tfs]
        self._avg = statistics.mean(self._lengths) if self._lengths else 0.0
        df: Counter[str] = Counter(term for tf in self._tfs for term in tf)
        n = len(texts)
        self._idf = {term: math.log(1 + (n - d + 0.5) / (d + 0.5)) for term, d in df.items()}

    def scores(self, query: str) -> list[float]:
        terms = [t for t in dict.fromkeys(bm25_tokens(query)) if t in self._idf]
        result: list[float] = []
        for tf, length in zip(self._tfs, self._lengths, strict=True):
            norm = self._k1 * (1 - self._b + self._b * length / self._avg) if self._avg else 0.0
            result.append(
                sum(self._idf[t] * tf[t] * (self._k1 + 1) / (tf[t] + norm) for t in terms if tf[t])
            )
        return result

    def search(self, query: str, k: int) -> list[tuple[int, float]]:
        """점수가 0보다 큰 청크만, 점수 내림차순(동점은 청크 순서)."""
        scored = [(i, s) for i, s in enumerate(self.scores(query)) if s > 0]
        return sorted(scored, key=lambda item: (-item[1], item[0]))[:k]


def rrf_fuse(
    rankings: Sequence[Sequence[tuple[int, float]]], rrf_k: int
) -> list[tuple[int, float]]:
    """순위 목록들을 Reciprocal Rank Fusion(Σ 1/(rrf_k + 순위))으로 합친다. 동점은 청크 순서."""
    fused: dict[int, float] = {}
    for ranking in rankings:
        for rank, (index, _) in enumerate(ranking, start=1):
            fused[index] = fused.get(index, 0.0) + 1.0 / (rrf_k + rank)
    return sorted(fused.items(), key=lambda item: (-item[1], item[0]))


class HybridRetriever:
    """dense와 BM25 각각 상위 ``depth``개를 RRF로 합친다."""

    def __init__(self, dense: Retriever, bm25: Retriever, rrf_k: int, depth: int) -> None:
        self._dense = dense
        self._bm25 = bm25
        self._rrf_k = rrf_k
        self._depth = depth

    def search(self, query: str, k: int) -> list[tuple[int, float]]:
        rankings = [self._dense.search(query, self._depth), self._bm25.search(query, self._depth)]
        return rrf_fuse(rankings, self._rrf_k)[:k]


def build_retriever(
    variant: VariantConfig,
    embedder: Embedder,
    chunks: Sequence[CorpusChunk],
    documents: Sequence[ProductDocument],
    cache: EmbeddingCache,
    clock: Callable[[], float] = time.perf_counter,
) -> tuple[Retriever, dict[str, Any]]:
    """variant의 검색기와 색인 정보(캐시 적중, 임베딩 시간 등)를 만든다."""
    if variant.retrieval == "dense":
        inputs = with_prefix(chunks, documents, variant.chunk_prefix)
        vectors, info = cache.get_or_compute(
            inputs, variant.chunk_prefix, embedder.embed_documents, clock
        )
        return DenseRetriever(embedder, vectors), {"dense": info}
    if variant.retrieval == "dense_bm25_rrf":
        # dense는 ①과 같은 prefix 없는 입력(캐시 공유), BM25는 같은 청크 본문.
        inputs = with_prefix(chunks, documents, "none")
        vectors, info = cache.get_or_compute(inputs, "none", embedder.embed_documents, clock)
        start = clock()
        bm25 = BM25Index([c.text for c in chunks], variant.bm25_k1, variant.bm25_b)
        bm25_seconds = round(clock() - start, 3)
        retriever = HybridRetriever(
            DenseRetriever(embedder, vectors), bm25, variant.rrf_k, variant.rrf_depth
        )
        return retriever, {"dense": info, "bm25_seconds": bm25_seconds}
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


def evaluate(
    retriever: Retriever,
    chunks: Sequence[CorpusChunk],
    names: Mapping[str, str],
    questions: Sequence[Question],
    checks: Sequence[GoldCheck],
    k: int,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    """유효한 문항을 검색·채점한다. 지연은 워밍업 질의 1회 뒤부터 잰다."""
    check_by_id = {c.question_id: c for c in checks}
    retriever.search(WARMUP_QUERY, k)
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
    return {
        "metrics": {
            "all": aggregate(rows, k),
            "namesProduct=true": aggregate([r for r in rows if r["names_product"]], k),
            "namesProduct=false": aggregate([r for r in rows if not r["names_product"]], k),
        },
        "latency_ms": latency_summary(latencies),
        "rows": rows,
    }


def run_variants(
    variants: Sequence[VariantConfig],
    documents: Sequence[ProductDocument],
    questions: Sequence[Question],
    checks: Sequence[GoldCheck],
    embedder: Embedder,
    cache: EmbeddingCache,
    *,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    """청크를 한 번 만들어 모든 variant를 같은 문항으로 측정한다.

    모델 로딩·워밍업은 호출 전에 끝내야 첫 질의 지연에 섞이지 않는다.
    """
    if not variants:
        raise ValueError("no variants")
    first = variants[0]
    if any(v.chunker != first.chunker or v.top_k != first.top_k for v in variants):
        raise ValueError("variants must share chunker config and top_k (same chunk list)")
    k = first.top_k
    names = {d.doc_id: d.name for d in documents}
    chunks = build_chunks(documents, replace(first, chunk_prefix="none"), embedder.count_tokens)
    body_hash = texts_sha256([c.text for c in chunks])

    results: dict[str, Any] = {}
    for variant in variants:
        start = clock()
        retriever, index = build_retriever(variant, embedder, chunks, documents, cache, clock)
        index["build_seconds"] = round(clock() - start, 3)
        # 근거 판정과 색인은 같은 청크 본문 목록을 써야 한다.
        variant_hash = texts_sha256([c.text for c in with_prefix(chunks, documents, "none")])
        if variant_hash != body_hash:
            raise RuntimeError(f"variant {variant.key} chunk texts differ")
        results[variant.key] = {
            "variant": variant.as_dict(),
            "chunk_text_sha256": variant_hash,
            "index": index,
            **evaluate(retriever, chunks, names, questions, checks, k, clock),
        }

    chunk_counts: dict[str, int] = {}
    for chunk in chunks:
        chunk_counts[chunk.doc_id] = chunk_counts.get(chunk.doc_id, 0) + 1
    with_section = sum(1 for c in chunks if c.section_title)
    invalid = [
        {"id": c.question_id, "doc_id": c.doc_id, "reason": c.reason} for c in checks if not c.valid
    ]
    return {
        "top_k": k,
        "chunker": asdict(first.chunker),
        "chunk_text_sha256": body_hash,
        "corpus": {
            "files": sum(len(d.files) for d in documents),
            "documents": len(documents),
            "chunks": len(chunks),
            "chunks_with_section_title": with_section,
            "section_title_ratio": round(with_section / len(chunks), 4) if chunks else 0.0,
            "detail": [{**d.summary(), "chunks": chunk_counts.get(d.doc_id, 0)} for d in documents],
        },
        "questions": {
            "total": len(questions),
            "evaluated": len(results[first.key]["rows"]),
            "invalid": invalid,
        },
        "variants": results,
    }


def is_failure(row: Mapping[str, Any]) -> bool:
    """정답 문서가 1위가 아니거나 근거 적중을 놓친 문항."""
    return row["doc_rank"] != 1 or not row["evidence@k"]


def question_table(
    report: Mapping[str, Any], categories: Mapping[str, str]
) -> list[dict[str, Any]]:
    """문항별 분류와 variant별 정답 문서 순위·근거 적중."""
    variants = report["variants"]
    by_key = {key: {r["id"]: r for r in result["rows"]} for key, result in variants.items()}
    first = next(iter(variants.values()))
    return [
        {
            "id": row["id"],
            "names_product": row["names_product"],
            "category": categories.get(row["id"], "-"),
            "rank": {key: by_key[key][row["id"]]["doc_rank"] for key in variants},
            "evidence": {key: bool(by_key[key][row["id"]]["evidence@k"]) for key in variants},
        }
        for row in first["rows"]
    ]


def category_summary(
    report: Mapping[str, Any],
    categories: Mapping[str, str],
    order: Sequence[str],
    baseline: str = "1",
) -> list[dict[str, Any]]:
    """기준 variant 실패 문항의 분류별 고친 수와, 기준에서 맞던 문항 중 새로 틀린 수."""
    variants = report["variants"]
    by_key = {key: {r["id"]: r for r in result["rows"]} for key, result in variants.items()}
    base = by_key[baseline]
    others = [key for key in variants if key != baseline]
    groups: list[dict[str, Any]] = []
    for category in order:
        ids = [
            qid for qid, row in base.items() if is_failure(row) and categories.get(qid) == category
        ]
        groups.append(
            {
                "category": category,
                "n": len(ids),
                "fixed": {key: sum(not is_failure(by_key[key][q]) for q in ids) for key in others},
                "new_failures": {key: 0 for key in others},
            }
        )
    passed = [qid for qid, row in base.items() if not is_failure(row)]
    groups.append(
        {
            "category": "① 성공(분류 없음)",
            "n": len(passed),
            "fixed": {key: 0 for key in others},
            "new_failures": {
                key: sum(is_failure(by_key[key][q]) for q in passed) for key in others
            },
        }
    )
    return groups


# 선택 규칙: (지표, 큰 값이 좋은가, 허용 차이).
# 허용 차이 None은 문항 1개(1/n), 지연은 최저값 대비 비율.
SELECTION_STEPS: tuple[tuple[str, bool, float | None], ...] = (
    ("recall@{k}", True, None),
    ("evidence@{k}", True, None),
    ("mrr@{k}", True, 0.034),
    ("latency_p50_ms", False, 0.20),
)
_EPS = 1e-9


def select_variant(report: Mapping[str, Any]) -> dict[str, Any]:
    """미리 정한 선택 규칙을 적용한다. 각 단계에서 최고값과 차이가 허용 이내인 후보를 남기고,
    마지막에 남은 후보 중 가장 단순한 것(VARIANTS 선언 순서)을 고른다. 지연 허용은 최저값의 20%."""
    variants = report["variants"]
    n = report["questions"]["evaluated"]
    candidates = list(variants)
    steps: list[dict[str, Any]] = []
    for template, higher, tolerance in SELECTION_STEPS:
        metric = template.format(k=report["top_k"])
        values = {
            key: (
                variants[key]["latency_ms"]["p50"]
                if metric == "latency_p50_ms"
                else variants[key]["metrics"]["all"][metric]
            )
            for key in candidates
        }
        if higher:
            best = max(values.values())
            allowed = tolerance if tolerance is not None else 1 / n
            kept = [key for key in candidates if best - values[key] <= allowed + _EPS]
        else:
            best = min(values.values())
            allowed = best * (tolerance or 0.0)
            kept = [key for key in candidates if values[key] - best <= allowed + _EPS]
        steps.append(
            {"metric": metric, "values": values, "best": best, "allowed": allowed, "kept": kept}
        )
        candidates = kept
    order = list(VARIANTS)
    selected = min(candidates, key=lambda key: order.index(key) if key in order else len(order))
    return {"steps": steps, "remaining": candidates, "selected": selected}


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def render_markdown(report: Mapping[str, Any]) -> str:
    """JSON 보고서를 사람이 읽는 Markdown으로 만든다."""
    k = report["top_k"]
    model = report["model"]
    corpus = report["corpus"]
    questions = report["questions"]
    variants = report["variants"]
    lines = [
        f"# 금융상품 설명서 검색 평가 — variant {', '.join(variants)}",
        "",
        "## 측정 조건",
        "",
        f"- 평가 시각: {report['evaluated_at']}",
        f"- 평가 파일: `{report['questions_file']}` (version {report['questions_version']})",
        f"- 청커: {report['chunker']}, top_k={k}",
        f"- 임베딩: {model['model_name']} revision={model['resolved_revision']} "
        f"(요청 {model['revision']}), device={model['device']}, "
        f"sentence-transformers {model['sentence_transformers']}, torch {model['torch']}",
        f"- 모델 로드+워밍업: {_fmt(model.get('load_seconds'))} s",
        f"- 말뭉치: PDF {corpus['files']}개 → 문서 {corpus['documents']}개, "
        f"청크 {corpus['chunks']}개, 청크 본문 sha256 {report['chunk_text_sha256'][:16]}…",
        f"- 섹션 제목이 있는 청크: {corpus['chunks_with_section_title']}/{corpus['chunks']} "
        f"({corpus['section_title_ratio']:.1%})",
        f"- 문항: 전체 {questions['total']}, 평가 {questions['evaluated']}, "
        f"invalid {len(questions['invalid'])}",
        "",
        "| variant | 설명 | 청크 본문 해시 같음 | 색인 | 질의 지연 중앙값 ms | p95 ms |",
        "|---|---|---|---|---|---|",
    ]
    for key, result in variants.items():
        same = result["chunk_text_sha256"] == report["chunk_text_sha256"]
        lines.append(
            f"| {key} | {result['variant']['description']} | {'O' if same else 'X'} | "
            f"{_index_text(result['index'])} | {_fmt(result['latency_ms']['p50'])} | "
            f"{_fmt(result['latency_ms']['p95'])} |"
        )
    lines += [
        "",
        "## 지표",
        "",
        f"| variant | 집단 | n | Recall@1 | Recall@{k} | MRR@{k} | 근거 적중@{k} |",
        "|---|---|---|---|---|---|---|",
    ]
    for key, result in variants.items():
        for group, metrics in result["metrics"].items():
            lines.append(
                f"| {key} | {group} | {metrics['n']} | {_fmt(metrics.get('recall@1'))} | "
                f"{_fmt(metrics.get(f'recall@{k}'))} | {_fmt(metrics.get(f'mrr@{k}'))} | "
                f"{_fmt(metrics.get(f'evidence@{k}'))} |"
            )
    lines += ["", "## invalid 문항(집계 제외)", ""]
    if questions["invalid"]:
        lines += [f"- {item['id']}: {item['reason']}" for item in questions["invalid"]]
    else:
        lines.append("- 없음")
    selection = report.get("selection")
    if selection:
        lines += ["", "## 선택 규칙 적용", "", f"- {report['selection_note']}"]
        for step in selection["steps"]:
            values = ", ".join(f"{key}={_fmt(v)}" for key, v in step["values"].items())
            lines.append(
                f"- {step['metric']}: {values} / 최고 {_fmt(step['best'])}, 허용 차이 "
                f"{_fmt(step['allowed'])} → 남은 후보 {', '.join(step['kept'])}"
            )
        lines.append(
            f"- 남은 후보 {', '.join(selection['remaining'])} 중 가장 단순한 것: "
            f"**{selection['selected']}**"
        )
    table = report.get("question_table")
    if table:
        keys = list(variants)
        lines += [
            "",
            "## 문항별 비교",
            "",
            "| 문항 | 상품명 | ① 분류 | "
            + " | ".join(f"{key} 순위" for key in keys)
            + " | "
            + " | ".join(f"{key} 근거" for key in keys)
            + " |",
            "|---|---|---|" + "---|" * (2 * len(keys)),
        ]
        for row in table:
            names = "있음" if row["names_product"] else "없음"
            lines.append(
                f"| {row['id']} | {names} | {row['category']} | "
                + " | ".join(_fmt(row["rank"][key]) for key in keys)
                + " | "
                + " | ".join("O" if row["evidence"][key] else "X" for key in keys)
                + " |"
            )
    summary = report.get("category_summary")
    if summary:
        others = list(summary[0]["fixed"])
        lines += [
            "",
            "## 분류별 요약(① 기준)",
            "",
            "| 분류 | 문항 수 | "
            + " | ".join(f"{key} 고침 | {key} 새로 틀림" for key in others)
            + " |",
            "|---|---|" + "---|---|" * len(others),
        ]
        for group in summary:
            lines.append(
                f"| {group['category']} | {group['n']} | "
                + " | ".join(
                    f"{group['fixed'][key]} | {group['new_failures'][key]}" for key in others
                )
                + " |"
            )
    lines += ["", f"## 문항별 상위 {k}", ""]
    for key, result in variants.items():
        lines += [f"### variant {key}", ""]
        for row in result["rows"]:
            hits = "; ".join(
                f"{hit['rank']}. {hit['document']} p.{_fmt(hit['page'])}"
                + (" [정답]" if hit["gold_doc"] else "")
                + (" [근거]" if hit["quote_hit"] else "")
                for hit in row["hits"]
            )
            lines.append(f"- {row['id']} (순위 {_fmt(row['doc_rank'])}): {hits}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _index_text(index: Mapping[str, Any]) -> str:
    parts = []
    dense = index.get("dense")
    if dense:
        parts.append(
            f"dense 임베딩 {_fmt(dense['embedding_seconds'])} s(캐시 {dense['cache']}, "
            f"prefix {dense['key'].rsplit('-', 1)[-1]})"
        )
    for name, value in index.items():
        if name not in {"dense", "build_seconds"}:
            parts.append(f"{name} {_fmt(value)}")
    parts.append(f"이번 실행 {_fmt(index['build_seconds'])} s")
    return ", ".join(parts)

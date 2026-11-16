"""상품설명서 검색 순위(#47 ③: BGE-M3 dense 20 + BM25 20, RRF)로 조건표 상품의 순서를 정한다.

챗봇 상품 추천 도구(#65)의 순서 B. 조건은 추천기(app/products/recommender.py)가 정하고,
검색은 순서만 정한다. 검색 후보(청크 40개)에 안 걸린 상품은 조건표 순서로 뒤에 붙인다
(specs/rag/product_recommend_v1_results.md 12절). 상품설명서 코퍼스(PRODUCT_CORPUS_DIR)와 로드된
임베딩 모델이 있어야 만들 수 있다. 없으면 도구가 조건표 순서(A)를 쓴다.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from app.rag.embeddings import Embedder

if TYPE_CHECKING:
    from app.rag.experiments.product_eval import Retriever


def rank_products(fused: Sequence[tuple[str, float]], excluded: set[str], k: int = 5) -> list[str]:
    """(문서 id, 점수) 청크 순위에서 excluded 문서를 빼고 문서별 최고 점수로 상위 k 문서를 고른다.

    동점은 먼저 나온 청크 순서(입력 순서)로 정한다.
    """
    best: dict[str, tuple[float, int]] = {}
    for order, (doc_id, score) in enumerate(fused):
        if doc_id in excluded:
            continue
        if doc_id not in best or score > best[doc_id][0]:
            best[doc_id] = (score, best.get(doc_id, (score, order))[1])
    ranked = sorted(best.items(), key=lambda item: (-item[1][0], item[1][1]))
    return [doc_id for doc_id, _ in ranked[:k]]


def search_order(ranked: Sequence[str], catalog_order: Sequence[str]) -> list[str]:
    """순서 B: 검색에 걸린 상품을 점수 순으로, 안 걸린 상품을 조건표 순서로 뒤에 붙인다."""
    found = [pid for pid in ranked if pid in catalog_order]
    return found + [pid for pid in catalog_order if pid not in found]


class ProductSearch:
    """메모리 안 ③ 검색기. ``rank``는 검색어로 상품 문서 id를 점수 순으로 돌려준다(약관 제외)."""

    def __init__(self, search: Retriever, chunk_doc_ids: Sequence[str], terms: set[str]) -> None:
        self._search = search
        self._chunk_doc_ids = list(chunk_doc_ids)
        self._terms = terms

    @classmethod
    def build(
        cls,
        corpus_dir: Path,
        embedder: Embedder,
        cache_dir: Path | None,
        model_name: str,
        revision: str,
    ) -> ProductSearch:
        from app.rag.experiments.product_corpus import load_corpus
        from app.rag.experiments.product_eval import (
            VARIANTS,
            EmbeddingCache,
            build_chunks,
            build_retriever,
        )

        documents = load_corpus(corpus_dir)
        terms = {d.doc_id for d in documents if d.kind == "terms"}
        variant = VARIANTS["3"]
        chunks = build_chunks(documents, variant, embedder.count_tokens)
        cache = EmbeddingCache(cache_dir, model_name, revision)
        retriever, _ = build_retriever(variant, embedder, chunks, documents, cache)
        return cls(retriever, [c.doc_id for c in chunks], terms)

    def rank(self, query: str) -> list[str]:
        fused = self._search.search(query, len(self._chunk_doc_ids))
        pairs = [(self._chunk_doc_ids[i], score) for i, score in fused]
        return rank_products(pairs, self._terms, k=len(self._chunk_doc_ids))

"""금융 정책 메모리 안 검색(이슈 #88, 저장 방식 B).

2단계 검색 비교에서 고른 방식(조문 단위 + BM25 RRF)을 쓴다. 검색어에 한글이 없으면 BM25를 빼고
dense만 쓴다(영어 검색어에서 BM25가 영어 가이드북 문장만 맞혀 순위를 망쳤다, 설계 문서 14절).
현행 조문만 색인한다(말뭉치가 시행 예정 판을 이미 뺐다). 서버 시작 때 문서 폴더
(FINANCE_POLICY_CORPUS_DIR)와 로드된 임베딩 모델로 만든다. 없으면 도구가 SEARCH_NOT_CONFIGURED.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.finance_policy.chunks import PolicyChunk, article_heads, unit_chunks
from app.finance_policy.corpus import Corpus, load_corpus
from app.rag.embeddings import Embedder

if TYPE_CHECKING:
    from app.rag.experiments.product_eval import Retriever

_HANGUL = re.compile(r"[가-힣]")
# 2단계에서 고른 청크 방식. True면 ④(나눈 조문 조각마다 조 머리). 설계 문서 15절 결정을 따른다.
ARTICLE_HEADS = False
RRF_K = 60
RRF_DEPTH = 20


def has_hangul(text: str) -> bool:
    return bool(_HANGUL.search(text))


@dataclass(frozen=True)
class PolicyHit:
    """도구 결과 한 건: 문서 제목·번호·조문(또는 쪽)·시행일·원문."""

    document: str
    number: str | None
    ref: str
    effective_from: str | None
    source_type: str
    text: str
    chunk_id: str

    def as_result(self) -> dict[str, Any]:
        out: dict[str, Any] = {"document": self.document, "ref": self.ref, "quote": self.text}
        if self.number:
            out["number"] = self.number
        if self.source_type in ("LAW", "ADMIN_RULE"):
            out["effectiveFrom"] = self.effective_from
        else:
            out["issuedOn"] = self.effective_from
        return out


def _ref(chunk: PolicyChunk) -> str:
    unit = chunk.unit_ids[0] if chunk.unit_ids else ""
    if chunk.doc in ("GUIDE", "PRESS"):
        return f"p.{chunk.page}" if chunk.page is not None else ""
    return unit.split(":")[1] if ":" in unit else unit


class FinancePolicySearch:
    """``search(query, k)``는 현행 조문·가이드 청크를 점수 순으로 돌려준다."""

    def __init__(
        self,
        chunks: Sequence[PolicyChunk],
        dense: Retriever,
        hybrid: Retriever,
        documents: Mapping[str, Mapping[str, Any]],
    ) -> None:
        self._chunks = list(chunks)
        self._dense = dense
        self._hybrid = hybrid
        self._documents = documents

    @classmethod
    def build(
        cls,
        corpus_dir: Path,
        embedder: Embedder,
        cache_dir: Path | None,
        model_name: str,
        revision: str,
        corpus: Corpus | None = None,
    ) -> FinancePolicySearch:
        from app.rag.experiments.product_eval import (
            BM25Index,
            CorpusChunk,
            DenseRetriever,
            EmbeddingCache,
            HybridRetriever,
        )

        corpus = corpus or load_corpus(corpus_dir)
        documents = {d["key"]: d for d in corpus.manifest["documents"]}
        heads = None
        if ARTICLE_HEADS:
            heads = article_heads(corpus.units, {k: d["title"] for k, d in documents.items()})
        chunks = unit_chunks(corpus.units, embedder.count_tokens, heads)
        wrapped = [
            CorpusChunk(c.chunk_id, c.doc, i, c.page, None, c.text, c.text)
            for i, c in enumerate(chunks)
        ]
        cache = EmbeddingCache(cache_dir, model_name, revision)
        vectors, _ = cache.get_or_compute(wrapped, "none", embedder.embed_documents)
        dense = DenseRetriever(embedder, vectors)
        bm25 = BM25Index([c.text for c in chunks], 1.5, 0.75)
        return cls(chunks, dense, HybridRetriever(dense, bm25, RRF_K, RRF_DEPTH), documents)

    def search(self, query: str, k: int) -> tuple[list[PolicyHit], str]:
        """(결과, 방식). 한글이 없으면 dense만(방식 'dense_only'), 있으면 'hybrid'."""
        mode = "hybrid" if has_hangul(query) else "dense_only"
        retriever = self._hybrid if mode == "hybrid" else self._dense
        hits = []
        for index, _ in retriever.search(query, k):
            chunk = self._chunks[index]
            doc = self._documents[chunk.doc]
            hits.append(
                PolicyHit(
                    document=doc["title"],
                    number=doc.get("number"),
                    ref=_ref(chunk),
                    effective_from=chunk.effective_from,
                    source_type=doc["sourceType"],
                    text=chunk.text,
                    chunk_id=chunk.chunk_id,
                )
            )
        return hits, mode

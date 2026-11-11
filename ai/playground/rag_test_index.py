"""테스트 채팅용 정책 문서 검색 인덱스(이 PC 메모리). 운영 RAG(MongoDB Vector Search)가 아니다.

- 매니페스트(`specs/rag/sources.json`)의 문서를 앱의 추출·청킹·BGE-M3 임베딩 코드로 처리한다.
- R2·MongoDB·MySQL에 아무것도 쓰지 않는다. 승인 절차를 거치지 않은 문서이므로 화면에
  "테스트 인덱스(승인 전 문서)"로 표시한다.
- 다른 문서가 대체(`relations.supersedes`)한 원본은 넣지 않는다.
- 임베딩은 파일 해시·모델·청킹 설정이 같으면 디스크 캐시를 다시 쓴다.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np

from app.rag.chunking import ChunkerConfig, approx_token_count, chunk_blocks
from app.rag.extractors import extract, extract_derived
from app.rag.extractors.base import Block
from app.rag.manifest import SourceEntry, load_manifest
from app.rag.sources import inspect_file, resolve_inside

AI_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = AI_ROOT / "specs" / "rag" / "sources.json"
CACHE_DIR = AI_ROOT / "playground" / ".cache"
TEXT_PREVIEW_CHARS = 800

TokenCounter = Callable[[str], int]
Progress = Callable[[str], None]


class Embedder(Protocol):
    """임베딩 모델 인터페이스(BgeM3Embedder, 테스트에서는 가짜)."""

    model_name: str

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


@dataclass(frozen=True)
class IndexedChunk:
    """인덱스에 넣은 청크 하나와 출처 정보."""

    policy_document_id: int | None
    title: str
    source_org: str | None
    source_url: str | None
    path: str
    page_number: int | None
    section_title: str | None
    text: str


@dataclass(frozen=True)
class SearchResult:
    """검색 결과 한 건: 순위, 유사도, 청크."""

    rank: int
    score: float
    chunk: IndexedChunk


@dataclass
class TestIndex:
    """이 PC 메모리에 올린 테스트 전용 검색 인덱스(청크와 벡터)."""

    chunks: list[IndexedChunk]
    vectors: np.ndarray
    skipped: list[tuple[str, str]] = field(default_factory=list)
    cache_hit: bool = False

    def search(self, query_vector: Sequence[float], top_k: int) -> list[SearchResult]:
        if not self.chunks:
            return []
        query = np.asarray(query_vector, dtype=np.float32)
        # 임베딩이 정규화돼 있으므로 내적 = 코사인 유사도.
        scores = self.vectors @ query
        order = np.argsort(-scores)[:top_k]
        return [
            SearchResult(rank=i + 1, score=float(scores[j]), chunk=self.chunks[j])
            for i, j in enumerate(order)
        ]


def active_entries(entries: Sequence[SourceEntry], domain: str) -> list[SourceEntry]:
    """해당 도메인 문서 중 다른 문서가 대체하지 않은 것."""
    superseded = {e.relations.supersedes for e in entries if e.relations.supersedes}
    return [e for e in entries if e.domain == domain and e.path not in superseded]


def _cache_key(entries: Sequence[SourceEntry], embedder: Embedder, config: ChunkerConfig) -> str:
    payload = {
        "files": sorted((e.path, e.sha256, [d.sha256 for d in e.derived_inputs]) for e in entries),
        "model": embedder.model_name,
        "revision": getattr(embedder, "revision", None),
        "chunker": asdict(config),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]


def _extract_blocks(entry: SourceEntry, root: Path) -> tuple[list[Block], str]:
    """매니페스트의 변환본(HWP → PDF 등)이 있으면 그것으로 추출한다(적재 스크립트와 같은 방식)."""
    if entry.derived_inputs:
        derived = entry.derived_inputs[0]
        info = inspect_file(root, derived.path)
        report = extract_derived(
            info.detected_kind, resolve_inside(root, derived.path).read_bytes()
        )
    else:
        info = inspect_file(root, entry.path)
        report = extract(info.detected_kind, resolve_inside(root, entry.path).read_bytes())
    if report.status != "ok":
        return [], f"{report.status}: {report.reason}"
    return report.blocks, ""


def build_index(
    embedder: Embedder,
    *,
    token_counter: TokenCounter = approx_token_count,
    manifest_path: Path = MANIFEST_PATH,
    cache_dir: Path | None = CACHE_DIR,
    domain: str = "visa",
    config: ChunkerConfig | None = None,
    progress: Progress | None = None,
) -> TestIndex:
    config = config or ChunkerConfig()
    manifest = load_manifest(manifest_path)
    root = manifest.resolve_root(manifest_path)
    entries = active_entries(manifest.sources, domain)
    key = _cache_key(entries, embedder, config)
    cache_file = cache_dir / f"{domain}-{key}.npz" if cache_dir is not None else None
    if cache_file is not None and cache_file.exists():
        with np.load(cache_file, allow_pickle=False) as data:
            meta = json.loads(str(data["meta"]))
            return TestIndex(
                chunks=[IndexedChunk(**c) for c in meta["chunks"]],
                vectors=data["vectors"],
                skipped=[tuple(s) for s in meta["skipped"]],
                cache_hit=True,
            )

    chunks: list[IndexedChunk] = []
    skipped: list[tuple[str, str]] = []
    for entry in entries:
        if progress is not None:
            progress(f"추출·청킹: {entry.title or entry.path}")
        try:
            blocks, reason = _extract_blocks(entry, root)
        except (OSError, ValueError) as exc:
            blocks, reason = [], f"{type(exc).__name__}: {exc}"
        if not blocks:
            skipped.append((entry.path, reason or "추출된 내용 없음"))
            continue
        for draft in chunk_blocks(blocks, token_counter, config):
            chunks.append(
                IndexedChunk(
                    policy_document_id=entry.policy_document_id,
                    title=entry.title or entry.path,
                    source_org=entry.source_org,
                    source_url=entry.source_url,
                    path=entry.path,
                    page_number=draft.page_number,
                    section_title=draft.section_title,
                    text=draft.text,
                )
            )

    if progress is not None:
        progress(f"임베딩: 청크 {len(chunks)}개")
    vectors = np.asarray(embedder.embed_documents([c.text for c in chunks]), dtype=np.float32)
    index = TestIndex(chunks=chunks, vectors=vectors.reshape(len(chunks), -1), skipped=skipped)
    if cache_file is not None:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        meta = {"chunks": [asdict(c) for c in chunks], "skipped": skipped}
        np.savez(
            cache_file, vectors=index.vectors, meta=np.array(json.dumps(meta, ensure_ascii=False))
        )
    return index


def search_payload(
    index: TestIndex, embedder: Embedder, query: str, top_k: int
) -> list[dict[str, object]]:
    """검색 도구가 Claude에게 돌려줄 결과. 본문은 앞부분만 넣는다."""
    hits = index.search(embedder.embed_query(query), top_k)
    return [
        {
            "rank": hit.rank,
            "score": round(hit.score, 4),
            "title": hit.chunk.title,
            "sourceOrg": hit.chunk.source_org,
            "page": hit.chunk.page_number,
            "section": hit.chunk.section_title,
            "url": hit.chunk.source_url,
            "policyDocumentId": hit.chunk.policy_document_id,
            "text": hit.chunk.text[:TEXT_PREVIEW_CHARS],
        }
        for hit in hits
    ]

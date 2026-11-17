"""금융 정책 말뭉치의 청크 방식 ①②(③은 ②와 같은 청크 + BM25).

- ① fixed: 문서마다 적재 범위 단위를 블록으로 넣어 chunk_blocks 기본값(600/900/80 토큰)으로 자른다.
- ② unit: 단위(조문·소제목) 하나가 청크 하나. 규칙(2026-11-17 결정, 측정 전 고정):
  30토큰 미만 단위는 같은 문서의 다음 단위에 붙이고, 900토큰 초과 단위는 줄(문단) 경계에서
  500토큰 이하로 나눈다.
청크마다 문서·단위·쪽·effective_from 메타를 둔다.
현행 단위만 받는다(시행 예정 판은 말뭉치에서 이미 뺐다).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.finance_policy.corpus import Unit
from app.rag.chunking import TokenCounter, chunk_blocks
from app.rag.extractors.base import Block

MERGE_BELOW_TOKENS = 30
SPLIT_ABOVE_TOKENS = 900
SPLIT_TARGET_TOKENS = 500


@dataclass(frozen=True)
class PolicyChunk:
    chunk_id: str
    doc: str
    text: str
    unit_ids: tuple[str, ...]
    page: int | None
    effective_from: str | None


def _order(units: Sequence[Unit]) -> list[str]:
    return list(dict.fromkeys(u.doc for u in units))


def fixed_chunks(units: Sequence[Unit], count: TokenCounter) -> list[PolicyChunk]:
    """① 문서별로 단위를 블록으로 넣어 기본 설정으로 자른다(조문 경계를 넘을 수 있음)."""
    out: list[PolicyChunk] = []
    for doc in _order(units):
        mine = [u for u in units if u.doc == doc]
        blocks = [Block(text=u.text, page_number=u.pages[0] if u.pages else None) for u in mine]
        effective = mine[0].effective_from if mine else None
        for draft in chunk_blocks(blocks, count):
            out.append(
                PolicyChunk(
                    f"fixed:{doc}#{draft.chunk_index}",
                    doc,
                    draft.text,
                    (),
                    draft.page_number,
                    effective,
                )  # fmt: skip
            )
    return out


def unit_chunks(units: Sequence[Unit], count: TokenCounter) -> list[PolicyChunk]:
    """② 단위 하나 = 청크 하나. 짧은 단위는 다음 단위에 붙이고, 긴 단위는 문단 경계에서 나눈다."""
    merged: list[tuple[str, str, tuple[str, ...], int | None, str | None]] = []
    carry: tuple[str, str, tuple[str, ...], int | None, str | None] | None = None
    for u in units:
        item: tuple[str, str, tuple[str, ...], int | None, str | None] = (
            u.doc,
            u.text,
            (u.unit_id,),
            u.pages[0] if u.pages else None,
            u.effective_from,
        )
        if carry is not None and carry[0] == u.doc:
            item = (u.doc, carry[1] + "\n" + u.text, carry[2] + item[2], carry[3], u.effective_from)
            carry = None
        elif carry is not None:
            merged.append(carry)
            carry = None
        if count(item[1]) < MERGE_BELOW_TOKENS:
            carry = item
            continue
        merged.append(item)
    if carry is not None:
        merged.append(carry)

    out: list[PolicyChunk] = []
    for doc, text, ids, page, effective in merged:
        parts = [text]
        if count(text) > SPLIT_ABOVE_TOKENS:
            parts = []
            current: list[str] = []
            for line in text.splitlines():
                if current and count("\n".join([*current, line])) > SPLIT_TARGET_TOKENS:
                    parts.append("\n".join(current))
                    current = []
                current.append(line)
            if current:
                parts.append("\n".join(current))
        for i, part in enumerate(parts):
            out.append(PolicyChunk(f"unit:{ids[0]}#{i}", doc, part, ids, page, effective))
    return out

"""금융 정책 검색 결과의 항 단위 라벨(이슈 #88 스펙 가드 ②).

라벨 표는 ``specs/rag/finance_policy_labels_v1.json``(2026-11-18 사용자 확정)이다. 조각(청크)이 걸친
항마다 라벨(direction: send/receive/both/none, appliesTo, note)을 붙이고, 문서마다 발행처를 붙인다.
FINANCE_POLICY_LABELS=true일 때만 도구 결과에 들어간다(기본 off: 결과는 v4.2 after와 같다).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.finance_policy.chunks import PolicyChunk
from app.finance_policy.corpus import Unit

LABELS_PATH = (
    Path(__file__).resolve().parents[2] / "specs" / "rag" / "finance_policy_labels_v1.json"
)
DIRECTIONS = frozenset({"send", "receive", "both", "none"})
# 항 표시: 줄 첫머리, 또는 조 제목 바로 뒤(예: "제2-31조(소액해외송금업자의업무) ① …").
_MARKER = re.compile(r"(?m)(?:^[ \t]*|^제[0-9-]+조(?:의\d+)?\([^)\n]*\)[ \t]*)([①-⑳])")

# (unit_id, 항 표시). 항 표시 앞부분(조 제목·호만 적재된 조문)은 ''.
Span = tuple[str, str]


def paragraph_marks(text: str) -> list[tuple[int, str]]:
    """(위치, 항 표시) 목록. 위치는 항 표시 글자의 위치."""
    return [(m.start(1), m.group(1)) for m in _MARKER.finditer(text)]


def _spans_in(text: str, start: int, end: int) -> list[str]:
    """text[start:end]가 걸친 항 표시(앞 항이 이어지면 그 항부터)."""
    current = ""
    out: list[str] = []
    for pos, mark in paragraph_marks(text):
        if pos <= start:
            current = mark
        elif pos < end:
            if not out:
                out.append(current)
            out.append(mark)
    if not out:
        out.append(current)
    return out


def chunk_paragraphs(
    chunks: Sequence[PolicyChunk], units: Iterable[Unit]
) -> dict[str, tuple[Span, ...]]:
    """청크마다 걸친 (unit_id, 항) 목록. 짧은 단위를 합친 청크는 단위마다, 나눈 조각은 걸친 항만."""
    by_id = {u.unit_id: u for u in units}
    out: dict[str, tuple[Span, ...]] = {}
    cursor: dict[tuple[str, ...], int] = {}
    for chunk in chunks:
        texts = [by_id[i].text if i in by_id else "" for i in chunk.unit_ids]
        merged = "\n".join(texts)
        body = chunk.text
        start = merged.find(body, cursor.get(chunk.unit_ids, 0))
        if start < 0 and "\n" in body:  # 조 머리를 앞에 붙인 조각(④ 후보)
            body = body.split("\n", 1)[1]
            start = merged.find(body, cursor.get(chunk.unit_ids, 0))
        if start < 0:
            out[chunk.chunk_id] = ()
            continue
        end = start + len(body)
        cursor[chunk.unit_ids] = end
        spans: list[Span] = []
        offset = 0
        for unit_id, text in zip(chunk.unit_ids, texts, strict=True):
            lo, hi = max(start, offset), min(end, offset + len(text))
            if lo < hi:
                spans += [(unit_id, mark) for mark in _spans_in(text, lo - offset, hi - offset)]
            offset += len(text) + 1
        out[chunk.chunk_id] = tuple(spans)
    return out


@dataclass(frozen=True)
class PolicyLabels:
    """라벨 표. ``for_hit``은 도구 결과 한 건에 더할 값(발행처, 걸친 항의 라벨 목록)."""

    issuers: Mapping[str, str]
    labels: Mapping[tuple[str, str, str], tuple[dict[str, Any], ...]]

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> PolicyLabels:
        grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for item in data["labels"]:
            if item["direction"] not in DIRECTIONS:
                raise ValueError(f"unknown direction: {item['direction']}")
            ref = f"{item['article']} {item['paragraph']}".strip()
            if item.get("scope"):
                ref += f" {item['scope']}"
            label: dict[str, Any] = {
                "ref": ref,
                "direction": item["direction"],
                "appliesTo": item["appliesTo"],
            }
            if item.get("note"):
                label["note"] = item["note"]
            key = (item["doc"], item["article"], item["paragraph"])
            grouped.setdefault(key, []).append(label)
        issuers = {doc: v["issuer"] for doc, v in data["issuers"].items()}
        return cls(issuers, {k: tuple(v) for k, v in grouped.items()})

    @classmethod
    def load(cls, path: Path = LABELS_PATH) -> PolicyLabels:
        return cls.from_data(json.loads(path.read_text(encoding="utf-8")))

    def for_spans(self, doc: str, spans: Sequence[Span]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if doc in self.issuers:
            out["issuer"] = self.issuers[doc]
        labels: list[dict[str, Any]] = []
        for unit_id, mark in spans:
            article = unit_id.split(":")[1] if ":" in unit_id else unit_id
            for label in self.labels.get((doc, article, mark), ()):
                if label not in labels:
                    labels.append(dict(label))
        if labels:
            out["labels"] = labels
        return out


@lru_cache(maxsize=1)
def default_labels() -> PolicyLabels:
    return PolicyLabels.load()

"""검색 평가 gold set 라벨링 보조: 질의마다 후보 청크를 뽑아 사람이 읽을 표로 만든다.

후보는 임베딩 유사도 순서일 뿐 정답이 아니다. 정답(gold)은 사람이 원문을 읽고 고른다
(``specs/rag/evaluation.json``의 규칙). DB·네트워크와 무관한 순수 함수만 둔다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt

NEEDS_GOLD = "needs_gold"
HEAD_CHARS = 90
LIST_CHARS = 110


@dataclass(frozen=True)
class Candidate:
    """한 질의의 후보 청크 하나."""

    rank: int
    score: float
    chunk: Mapping[str, Any]


def pending_cases(cases: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """정답을 아직 고르지 않은(needs_gold) 케이스. 계약 대기 등 다른 상태는 뺀다."""
    return [c for c in cases if c.get("status") == NEEDS_GOLD]


def _normalized(vectors: npt.ArrayLike) -> npt.NDArray[np.float32]:
    matrix = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    normalized: npt.NDArray[np.float32] = matrix / norms
    return normalized


def rank_candidates(
    query_vectors: npt.ArrayLike, chunk_vectors: npt.ArrayLike, top_k: int
) -> list[list[tuple[int, float]]]:
    """질의마다 코사인 유사도 상위 ``top_k``개의 (청크 위치, 점수). 점수가 같으면 앞 청크 먼저."""
    if top_k < 1:
        raise ValueError("top_k must be at least 1")
    scores = _normalized(query_vectors) @ _normalized(chunk_vectors).T
    ranked: list[list[tuple[int, float]]] = []
    for row in scores:
        order = np.argsort(-row, kind="stable")[:top_k]
        ranked.append([(int(i), float(row[i])) for i in order])
    return ranked


def superseded_ids(manifest: Mapping[str, Any]) -> dict[int, int]:
    """manifest ``relations.supersedes``에서 {대체된 문서 id: 대체한 문서 id}."""
    by_path = {s["path"]: s.get("policy_document_id") for s in manifest.get("sources", [])}
    result: dict[int, int] = {}
    for source in manifest.get("sources", []):
        old_path = (source.get("relations") or {}).get("supersedes")
        old_id, new_id = by_path.get(old_path or ""), source.get("policy_document_id")
        if isinstance(old_id, int) and isinstance(new_id, int):
            result[old_id] = new_id
    return result


def _one_line(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit].replace("|", "\\|")


def _doc_label(chunk: Mapping[str, Any], superseded: Mapping[int, int]) -> str:
    doc_id = chunk["policy_document_id"]
    label = f"{doc_id} {chunk['title']}"
    if doc_id in superseded:
        label += f" (대체됨 → {superseded[doc_id]}번)"
    return label


def gold_entry(chunk: Mapping[str, Any]) -> dict[str, Any]:
    """``evaluation.json``의 gold 한 항목 형식. evidence는 사람이 원문에서 인용해 채운다."""
    return {
        "policy_document_id": chunk["policy_document_id"],
        "document_version": chunk["document_version"],
        "chunk_index": chunk["chunk_index"],
        "page_number": None,
        "evidence": "원문에서 근거 문장 인용",
    }


def render_markdown(
    cases: Sequence[Mapping[str, Any]],
    candidates: Sequence[Sequence[Candidate]],
    chunks: Sequence[Mapping[str, Any]],
    *,
    superseded: Mapping[int, int],
    summary: str,
) -> str:
    """라벨링 표(Markdown). 질의별 후보 표·원문, gold 형식 예시, 문서별 청크 목록."""
    lines = [
        "# gold set 라벨링 후보 표",
        "",
        f"- {summary}",
        "- 후보는 임베딩 유사도 순서입니다. **모델 출력이라 정답이 아닙니다.** 원문을 읽고 근거가 "
        "되는 청크만 고르고, 후보에 없으면 맨 아래 '문서별 청크 목록'에서 찾으세요.",
        "- '대체됨'으로 표시된 문서는 이전 버전입니다. 같은 내용이 대체한 문서에 있으면 "
        "그쪽을 고르세요.",
        "- `kind`가 unanswerable인 질의는 문서에 답이 없어야 하는 질의입니다. "
        "후보가 실제로 답을 담고 있지 않은지 확인만 하세요.",
        "- 고른 뒤 `specs/rag/evaluation.json`의 해당 케이스에 `gold`를 채우고 `status`를 "
        "`ready`로 바꿉니다.",
        "",
    ]
    for case, picks in zip(cases, candidates, strict=True):
        lines += [
            f"## {case['id']} ({case.get('language')}, {case.get('kind')}, {case.get('area')})",
            "",
            f"> {case['query']}",
            "",
            "| 순위 | 점수 | 문서 | 버전 | chunk | 앞부분 |",
            "|---|---|---|---|---|---|",
        ]
        for p in picks:
            c = p.chunk
            lines.append(
                f"| {p.rank} | {p.score:.3f} | {_doc_label(c, superseded)} | "
                f"`{c['document_version']}` | {c['chunk_index']} | "
                f"{_one_line(c['chunk_text'], HEAD_CHARS)} |"
            )
        lines += ["", "<details><summary>후보 원문 펼치기</summary>", ""]
        for p in picks:
            c = p.chunk
            lines += [
                f"**{p.rank}. 문서 {c['policy_document_id']} / chunk {c['chunk_index']}** "
                f"({c['title']})",
                "",
                "```text",
                str(c["chunk_text"]).strip(),
                "```",
                "",
            ]
        lines += ["</details>", ""]
        if picks:
            lines += [
                "gold 형식(값은 고른 청크로 바꾸세요):",
                "",
                "```json",
                json.dumps(gold_entry(picks[0].chunk), ensure_ascii=False),
                "```",
                "",
            ]

    # gold 키가 (문서, 버전, chunk)이므로 버전별로 따로 나열한다. 섞으면 없는 조합을 고르게 된다.
    lines += ["## 문서별 청크 목록", ""]
    versions = sorted({(c["policy_document_id"], c["document_version"]) for c in chunks})
    for doc_id, version in versions:
        docs = sorted(
            (
                c
                for c in chunks
                if c["policy_document_id"] == doc_id and c["document_version"] == version
            ),
            key=lambda c: c["chunk_index"],
        )
        first = docs[0]
        lines += [
            f"### {_doc_label(first, superseded)} (`{first['document_version']}`, {len(docs)}개)",
            "",
        ]
        lines += [
            f"- chunk {c['chunk_index']}: {_one_line(c['chunk_text'], LIST_CHARS)}" for c in docs
        ]
        lines.append("")
    return "\n".join(lines)

"""gold set 라벨링 보조(app.rag.labeling) 단위 테스트. DB·모델 없이 실행한다."""

from __future__ import annotations

from typing import Any

import pytest

from app.rag.labeling import (
    Candidate,
    gold_entry,
    pending_cases,
    rank_candidates,
    render_markdown,
    superseded_ids,
)

pytestmark = pytest.mark.unit


def _chunk(doc_id: int, index: int, text: str = "본문") -> dict[str, Any]:
    return {
        "policy_document_id": doc_id,
        "document_version": f"V{doc_id}",
        "chunk_index": index,
        "chunk_text": text,
        "title": f"문서{doc_id}",
    }


def test_pending_cases_keeps_only_needs_gold() -> None:
    cases = [
        {"id": "a", "status": "needs_gold"},
        {"id": "b", "status": "ready"},
        {"id": "c", "status": "blocked_finance_contract"},
    ]
    assert [c["id"] for c in pending_cases(cases)] == ["a"]


def test_rank_candidates_orders_by_cosine_and_ignores_vector_length() -> None:
    chunks = [[1.0, 0.0], [0.0, 1.0], [10.0, 10.0]]
    ranked = rank_candidates([[3.0, 0.0], [0.0, 1.0]], chunks, top_k=2)
    assert [i for i, _ in ranked[0]] == [0, 2]
    assert [i for i, _ in ranked[1]] == [1, 2]
    assert ranked[0][0][1] == pytest.approx(1.0)
    assert ranked[0][1][1] == pytest.approx(2**-0.5)


def test_rank_candidates_ties_keep_chunk_order_and_cap_at_available() -> None:
    ranked = rank_candidates([[1.0, 0.0]], [[2.0, 0.0], [1.0, 0.0]], top_k=5)
    assert [i for i, _ in ranked[0]] == [0, 1]


def test_rank_candidates_rejects_zero_top_k() -> None:
    with pytest.raises(ValueError):
        rank_candidates([[1.0]], [[1.0]], top_k=0)


def test_superseded_ids_reads_manifest_relations() -> None:
    manifest = {
        "sources": [
            {"path": "new.pdf", "policy_document_id": 6, "relations": {"supersedes": "old.hwp"}},
            {"path": "old.hwp", "policy_document_id": 7, "relations": {"supersedes": None}},
            {"path": "fin.pdf", "policy_document_id": None, "relations": {"supersedes": "x"}},
        ]
    }
    assert superseded_ids(manifest) == {7: 6}


def test_gold_entry_matches_evaluation_format() -> None:
    assert gold_entry(_chunk(6, 0)) == {
        "policy_document_id": 6,
        "document_version": "V6",
        "chunk_index": 0,
        "page_number": None,
        "evidence": "원문에서 근거 문장 인용",
    }


def test_render_markdown_lists_candidates_marks_superseded_and_escapes_pipes() -> None:
    chunks = [_chunk(6, 0, "선발인원 | 33,000명"), _chunk(7, 0, "이전 버전"), _chunk(6, 1)]
    cases = [{"id": "en-02", "language": "en", "kind": "answerable", "area": "s", "query": "Q?"}]
    picks = [[Candidate(1, 0.9, chunks[1]), Candidate(2, 0.8, chunks[0])]]
    md = render_markdown(cases, picks, chunks, superseded={7: 6}, summary="요약")
    assert "## en-02 (en, answerable, s)" in md
    assert "> Q?" in md
    assert "| 1 | 0.900 | 7 문서7 (대체됨 → 6번) | `V7` | 0 | 이전 버전 |" in md
    assert "선발인원 \\| 33,000명" in md  # 표 칸을 깨지 않게
    assert '"policy_document_id": 7' in md  # gold 형식 예시
    # 문서별 목록은 문서 id, chunk 순서
    listing = md.split("## 문서별 청크 목록")[1]
    assert listing.index("### 6 문서6") < listing.index("### 7 문서7")
    assert listing.index("- chunk 0: 선발인원") < listing.index("- chunk 1: 본문")


def test_render_markdown_requires_candidates_for_every_case() -> None:
    cases = [{"id": "a", "query": "Q"}, {"id": "b", "query": "R"}]
    with pytest.raises(ValueError):
        render_markdown(cases, [[]], [], superseded={}, summary="")

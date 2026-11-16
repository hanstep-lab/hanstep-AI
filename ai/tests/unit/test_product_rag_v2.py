"""금융상품 검색 v2 평가 준비: v2 형식 로더, '.pdf' 매핑, 언어·겹침 집계, 비교 통계 단위 테스트."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.rag.experiments.product_corpus import (
    CorpusError,
    ProductDocument,
    Question,
    check_gold,
    load_questions,
)
from app.rag.experiments.product_eval import group_metrics
from app.rag.experiments.product_stats import (
    OVERLAP_NOT_APPLICABLE,
    bigram_jaccard,
    bootstrap_ci,
    build_statistics,
    exact_mcnemar,
    overlap_groups,
    pair_comparison,
    render_statistics,
    review_decision,
)
from app.rag.extractors.base import Block

pytestmark = pytest.mark.unit


def _v2(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "v2-001",
        "language": "en",
        "hasProductName": False,
        "question": "Can I?",
        "goldDocument": "정기적금.pdf",
        "goldQuote": "가입대상",
        "answerSummary": "Yes.",
    }
    return {**base, **overrides}


def _write(tmp_path: Path, payload: Any, name: str = "rag_holdout_v2.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_v2_loader_maps_fields_and_keeps_language_and_pair(tmp_path: Path) -> None:
    rows = [_v2(), _v2(id="v2k-001", language="ko", pairId="v2-001", hasProductName=True)]
    version, questions = load_questions(_write(tmp_path, rows))
    assert version == "rag_holdout_v2"
    assert questions[0] == Question(
        "v2-001", "Can I?", "정기적금.pdf", "가입대상", None, False, "en"
    )
    assert questions[1].pair_id == "v2-001" and questions[1].names_product is True


@pytest.mark.parametrize(
    "row",
    [
        {k: v for k, v in _v2().items() if k != "answerSummary"},
        _v2(language="jp"),
        _v2(hasProductName="no"),
        _v2(extra="x"),
    ],
)
def test_v2_loader_rejects_bad_rows(tmp_path: Path, row: dict[str, Any]) -> None:
    with pytest.raises(CorpusError):
        load_questions(_write(tmp_path, [row]))


def test_v2_loader_rejects_duplicate_ids(tmp_path: Path) -> None:
    with pytest.raises(CorpusError):
        load_questions(_write(tmp_path, [_v2(), _v2()]))


def _doc(name: str, text: str) -> ProductDocument:
    return ProductDocument(
        "d1", name, name, "product", "x", (f"{name}.pdf",), 1, (Block(text, page_number=1),)
    )


def test_gold_document_strips_trailing_pdf_only() -> None:
    docs = [_doc("정기적금", "가입대상 실명의 개인")]
    questions = [
        Question("a", "q", "정기적금.pdf", "가입대상", None, False),
        Question("b", "q", "정기적금", "가입대상", 1, False),
        Question("c", "q", "정기.pdf적금", "가입대상", None, False),
    ]
    checks = {c.question_id: c for c in check_gold(questions, docs)}
    assert checks["a"].valid and checks["b"].valid and not checks["c"].valid


def test_overlap_groups_only_for_korean_with_median_split() -> None:
    questions = [
        Question("k1", "가입대상 알려줘", "", "가입대상", None, False, "ko"),
        Question("k2", "전혀 다른 말", "", "가입대상", None, False, "ko"),
        Question("k3", "가입 대상", "", "가입대상 개인", None, False, "ko"),
        Question("e1", "who can join", "", "가입대상", None, False, "en"),
    ]
    groups = overlap_groups(questions)
    assert groups["e1"]["group"] == OVERLAP_NOT_APPLICABLE and groups["e1"]["jaccard"] is None
    assert groups["k2"]["group"] == "낮음"
    assert {groups["k1"]["group"], groups["k3"]["group"]} == {"높음"}
    assert bigram_jaccard("가입 대상", "가입대상") == 1.0


def _row(
    qid: str, rank: int | None, evidence: float, language: str | None = None, **extra: Any
) -> dict[str, Any]:
    return {
        "id": qid,
        "names_product": False,
        "language": language,
        "pair_id": extra.get("pair_id"),
        "overlap": extra.get("overlap", {"group": OVERLAP_NOT_APPLICABLE}),
        "doc_rank": rank,
        "recall@1": 1.0 if rank == 1 else 0.0,
        "recall@k": 1.0 if rank is not None else 0.0,
        "rr": 1.0 / rank if rank else 0.0,
        "evidence@k": evidence,
    }


def test_group_metrics_adds_language_and_korean_overlap_groups() -> None:
    rows = [
        _row("k1", 1, 1.0, "ko", overlap={"group": "낮음"}),
        _row("k2", None, 0.0, "ko", overlap={"group": "높음"}),
        _row("e1", 2, 1.0, "en"),
    ]
    groups = group_metrics(rows, 5)
    assert groups["language=ko"]["n"] == 2 and groups["language=en"]["n"] == 1
    assert groups["overlap(ko)=낮음"]["recall@1"] == 1.0
    assert groups["overlap(ko)=높음"]["recall@5"] == 0.0
    assert "language=ko" not in group_metrics([_row("s1", 1, 1.0)], 5)


@pytest.mark.parametrize(
    ("b", "c", "expected"),
    [(0, 0, 1.0), (0, 2, 0.5), (1, 7, 0.0703125), (0, 10, 2 / 1024)],
)
def test_exact_mcnemar(b: int, c: int, expected: float) -> None:
    assert exact_mcnemar(b, c) == pytest.approx(expected)


def test_bootstrap_is_seeded_and_brackets_mean() -> None:
    values = {"1": [1.0, 0.0, 1.0, 1.0], "3": [1.0, 1.0, 1.0, 1.0]}
    first = bootstrap_ci(values, rounds=500, seed=7)
    assert first == bootstrap_ci(values, rounds=500, seed=7)
    assert first["1"]["low"] <= first["1"]["mean"] <= first["1"]["high"]
    assert first["3"] == {"mean": 1.0, "low": 1.0, "high": 1.0}


def _report(rows: dict[str, list[dict[str, Any]]], source: str = "q.json") -> dict[str, Any]:
    return {"questions_file": source, "variants": {key: {"rows": r} for key, r in rows.items()}}


def test_build_statistics_splits_languages_pairs_and_decides() -> None:
    v1 = _report({key: [_row("s1", 1, 1.0), _row("s2", None, 0.0)] for key in ("1", "2", "3")})
    v2_rows = {
        "1": [
            _row("e1", 1, 1.0, "en"),
            _row("k1", 1, 1.0, "ko", pair_id="e1"),
            _row("k2", 1, 1.0, "ko"),
        ],
        "2": [
            _row("e1", 2, 1.0, "en"),
            _row("k1", 1, 1.0, "ko", pair_id="e1"),
            _row("k2", 1, 1.0, "ko"),
        ],
        "3": [
            _row("e1", None, 0.0, "en"),
            _row("k1", None, 0.0, "ko", pair_id="e1"),
            _row("k2", None, 0.0, "ko"),
        ],
    }
    stats = build_statistics({"v1-fixed": v1, "v2": _report(v2_rows)}, rounds=200, seed=1)
    assert list(stats["sets"]) == ["v1-fixed", "v2-en", "v2-ko", "v2"]
    assert stats["sets"]["v2-ko"]["n"] == 2
    assert stats["sets"]["v2-ko"]["comparisons"]["1vs3"]["recall@k"] == {
        "b": 2,
        "c": 0,
        "p": 0.5,
        "significant": False,
    }
    assert stats["pairs"]["v2"]["1"]["both_top1"] == 1
    assert stats["decision"]["decision"] == "재검토"
    markdown = render_statistics(stats)
    assert "## v2-en (n=1) — 실사용 언어 조건" in markdown
    assert "유의하지 않음" in markdown and "## 측정 전 코드 수정(공개)" in markdown


def test_review_decision_needs_two_fewer_hits() -> None:
    one_less = {"hits": {"1": {"recall@k": 90}, "3": {"recall@k": 89}}}
    two_less = {"hits": {"1": {"recall@k": 90}, "3": {"recall@k": 88}}}
    assert review_decision(one_less)["decision"] == "선택 ③ 유지"
    assert review_decision(two_less)["decision"] == "재검토"


def test_pair_comparison_counts_rank_direction() -> None:
    rows = {"3": {"k": _row("k", 1, 1.0), "e": _row("e", None, 0.0)}}
    summary = pair_comparison(rows, [("k", "e")])["3"]
    assert (summary["ko_better"], summary["en_better"], summary["same"]) == (1, 0, 0)
    assert summary["top5"] == {"ko_only": 1, "en_only": 0, "both": 0, "neither": 0}


def test_build_statistics_records_excluded_questions() -> None:
    report = _report({key: [_row("s1", 1, 1.0)] for key in ("1", "3")})
    report["excluded"] = [{"id": "v2k-043", "reason": "s14와 거의 같은 질문"}]
    stats = build_statistics({"v2": report}, rounds=10, seed=1)
    assert stats["excluded"]["v2"] == [{"id": "v2k-043", "reason": "s14와 거의 같은 질문"}]
    assert "- v2 v2k-043: s14와 거의 같은 질문" in render_statistics(stats)

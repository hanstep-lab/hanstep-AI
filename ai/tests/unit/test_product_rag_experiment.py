"""금융상품 검색 오프라인 실험(app.rag.experiments) 단위 테스트. 모델·PDF 없이 실행한다."""

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
    match_key,
    parse_title,
)
from app.rag.experiments.product_eval import (
    VARIANTS,
    CorpusChunk,
    DenseRetriever,
    aggregate,
    render_markdown,
    run_experiment,
    score_question,
)
from app.rag.extractors.base import Block
from tests.conftest import FakeEmbedder

pytestmark = pytest.mark.unit


def _document(doc_id: str, name: str, pages: list[str]) -> ProductDocument:
    blocks = tuple(Block(text, page_number=i) for i, text in enumerate(pages, start=1))
    return ProductDocument(
        doc_id=doc_id,
        name=name,
        title=f"{name} 상품설명서",
        kind="product",
        sha256=doc_id,
        files=(f"{name}.pdf",),
        pages=len(pages),
        blocks=blocks,
    )


def _question(qid: str, text: str, gold: str, quote: str, names: bool = False) -> Question:
    return Question(qid, text, gold, quote, 1, names)


def _chunk(doc_id: str, index: int, text: str = "본문") -> CorpusChunk:
    return CorpusChunk(f"{doc_id}#{index}", doc_id, index, 1, None, text, text)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("SOL글로벌 적금 상품설명서", ("SOL글로벌 적금", "product")),
        ("『신한 안녕, 반가워 적금』 상품설명서", ("신한 안녕, 반가워 적금", "product")),
        ("신한 My플러스 정기예금 상품설명서", ("신한 My플러스 정기예금", "product")),
        ("거치식예금약관", ("거치식예금약관", "terms")),
    ],
)
def test_parse_title_names_product_and_terms(line: str, expected: tuple[str, str]) -> None:
    assert parse_title(line) == expected


@pytest.mark.parametrize("line", ["상품 안내", "상품설명서"])
def test_parse_title_rejects_unknown_or_empty(line: str) -> None:
    with pytest.raises(CorpusError):
        parse_title(line)


def test_match_key_ignores_spaces_and_line_breaks() -> None:
    assert match_key("50만원 이상\n2,000만원  이하") == match_key("50만원이상 2,000만원 이하")


def test_check_gold_maps_document_and_marks_invalid_without_editing() -> None:
    docs = [
        _document("d1", "SOL글로벌 적금", ["해외 송금시 특별중도해지\n이율을 제공", "2쪽"]),
        _document("d2", "거치식예금약관", ["제1조 적용범위"]),
    ]
    questions = [
        _question("q1", "질문", "SOL 글로벌 적금", "특별중도해지 이율을 제공"),
        _question("q2", "질문", "없는 상품", "아무 문장"),
        _question("q3", "질문", "거치식예금약관", "문서에 없는 문장"),
    ]
    checks = {c.question_id: c for c in check_gold(questions, docs)}
    assert checks["q1"].valid and checks["q1"].doc_id == "d1"
    assert checks["q1"].quote_pages == (1,)
    assert not checks["q2"].valid and checks["q2"].doc_id is None
    assert not checks["q3"].valid and checks["q3"].doc_id == "d2"
    assert questions[2].gold_quote == "문서에 없는 문장"


def _write(tmp_path: Path, payload: Any) -> Path:
    path = tmp_path / "questions.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _raw(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "s01",
        "question": "질문",
        "goldDocument": "정기적금",
        "goldQuote": "근거",
        "page": 1,
        "namesProduct": False,
    }
    return {**base, **overrides}


def test_load_questions_reads_valid_file(tmp_path: Path) -> None:
    version, questions = load_questions(_write(tmp_path, {"version": "v1", "questions": [_raw()]}))
    assert version == "v1"
    assert questions == [Question("s01", "질문", "정기적금", "근거", 1, False)]


@pytest.mark.parametrize(
    "questions",
    [
        [_raw(page=True)],
        [_raw(namesProduct="false")],
        [{k: v for k, v in _raw().items() if k != "goldQuote"}],
        [_raw(), _raw()],
    ],
)
def test_load_questions_rejects_bad_schema(tmp_path: Path, questions: list[Any]) -> None:
    with pytest.raises(CorpusError):
        load_questions(_write(tmp_path, {"version": "v1", "questions": questions}))


def test_score_question_counts_document_once_for_mrr() -> None:
    top = [_chunk("a", 0), _chunk("a", 1), _chunk("gold", 3, "근거 문장이 여기")]
    score = score_question("gold", "근거 문장이\n여기", top)
    assert score["doc_rank"] == 2
    assert score["rr"] == 0.5
    assert score["recall@1"] == 0.0 and score["recall@k"] == 1.0
    assert score["evidence@k"] == 1.0 and score["evidence_chunks"] == ["gold#3"]


def test_score_question_evidence_requires_gold_document() -> None:
    top = [_chunk("other", 0, "근거 문장")]
    score = score_question("gold", "근거 문장", top)
    assert score["doc_rank"] is None and score["rr"] == 0.0 and score["evidence@k"] == 0.0


def test_aggregate_handles_empty_group() -> None:
    assert aggregate([], 5) == {"n": 0}


def test_dense_retriever_ranks_identical_text_first() -> None:
    chunks = [_chunk("a", 0, "가입 금액"), _chunk("b", 0, "중도해지 이율"), _chunk("c", 0, "만기")]
    ranked = DenseRetriever(FakeEmbedder(), chunks).search("중도해지 이율", 2)
    assert len(ranked) == 2
    assert ranked[0][0] == 1 and ranked[0][1] == pytest.approx(1.0)


def test_run_experiment_skips_invalid_and_splits_names_product() -> None:
    docs = [
        _document("d1", "정기적금", ["정기적금 가입대상 실명의 개인"]),
        _document("d2", "청년 처음적금", ["만 19세 이상 34세 이하 청년"]),
    ]
    questions = [
        _question("q1", "정기적금 가입대상 실명의 개인", "정기적금", "가입대상", names=True),
        _question("q2", "만 19세 이상 34세 이하 청년", "청년 처음적금", "34세 이하"),
        _question("q3", "질문", "정기적금", "없는 근거"),
    ]
    checks = check_gold(questions, docs)
    report = run_experiment(VARIANTS["1"], docs, questions, checks, FakeEmbedder())
    assert report["questions"]["evaluated"] == 2
    assert report["questions"]["invalid"] == [
        {"id": "q3", "doc_id": "d1", "reason": "goldQuote not in document text"}
    ]
    assert report["corpus"]["documents"] == 2 and report["corpus"]["chunks"] == 2
    assert report["metrics"]["all"]["recall@1"] == 1.0
    assert report["metrics"]["namesProduct=true"]["n"] == 1
    assert report["metrics"]["namesProduct=false"]["evidence@5"] == 1.0
    assert report["latency_ms"]["p50"] is not None

    full = {
        **report,
        "evaluated_at": "2026-11-16T00:00:00+00:00",
        "questions_file": "q.json",
        "questions_version": "v1",
        "model": {
            "model_name": "BAAI/bge-m3",
            "revision": "default-branch",
            "resolved_revision": "abc1234",
            "device": "cpu",
            "sentence_transformers": "x",
            "torch": "y",
        },
    }
    markdown = render_markdown(full)
    assert "## 측정 조건" in markdown and "abc1234" in markdown
    assert "| namesProduct=false | 1 |" in markdown
    assert "- q3: goldQuote not in document text" in markdown

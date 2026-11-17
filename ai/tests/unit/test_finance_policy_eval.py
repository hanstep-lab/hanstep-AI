"""금융 정책 RAG(#88) 평가 세트·기준선 도구: 질문 세트, fake 모드 실행, 확인표(API 없음)."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.integrations.policy_gateway import UnconfiguredPolicyGateway
from app.main import AppState, create_app
from app.rag.embeddings import BgeM3Embedder

pytestmark = pytest.mark.unit

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
TOKEN = "f" * 40


def _load(name: str) -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EVAL = _load("evaluate_chat")
REVIEW = _load("finance_policy_review")


def _qset() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(EVAL.POLICY_QUESTIONS.read_text(encoding="utf-8"))
    return data


def test_question_set_shape() -> None:
    qset = _qset()
    questions = qset["questions"]
    assert len(questions) == 24
    counts = {
        c: sum(q["category"] == c for q in questions)
        for c in ("answerable", "not_in_docs", "out_of_scope")
    }
    assert counts == {"answerable": 16, "not_in_docs": 4, "out_of_scope": 4}
    assert len({q["id"] for q in questions}) == 24
    for q in questions:
        assert q["history"] == [] and isinstance(q["expectedTools"], list)
        if q["category"] != "answerable":
            assert q["gold"] == [] and q["keyFacts"] == []
            continue
        assert q["gold"] and 1 <= len(q["keyFacts"]) <= 3
        assert q["queryKo"] and q["queryEn"]
        assert re.search(r"[가-힣]", q["queryKo"]) and not re.search(r"[가-힣]", q["queryEn"])
        for fact in q["keyFacts"]:
            assert fact["quote"] and fact["doc"] in qset["documents"]
            re.compile(fact["answerHint"])
    # 규정 질문은 v4.1 기준선에서 기대 도구가 없다(규정 도구가 아직 없음).
    assert all(q["expectedTools"] == [] for q in questions if q["category"] != "out_of_scope")


def _settings() -> Settings:
    values: dict[str, Any] = {"ai_internal_api_token": TOKEN, "chat_fake_delta_delay_ms": 0}
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def _client(settings: Settings) -> TestClient:
    app = create_app()
    app.state.rag = AppState(
        settings=settings, gateway=UnconfiguredPolicyGateway(), embedder=BgeM3Embedder()
    )
    return TestClient(app)


def test_fake_run_and_review_table(tmp_path: Path) -> None:
    settings = _settings()
    output = tmp_path / "policy.jsonl"
    argv = ["--base-url", "http://testserver", "--question-set", "policy"]
    argv += ["--questions", "A01,N02,O04", "--output", str(output)]
    code = EVAL.main(argv, client=_client(settings), settings=settings, confirm=lambda _: "n")
    assert code == 0
    rows = REVIEW.load_rows(output)
    assert [r["questionId"] for r in rows] == ["A01", "N02", "O04"]
    assert all(r["status"] == "done" for r in rows)

    review = tmp_path / "review.md"
    assert REVIEW.main([str(output), "--output", str(review)]) == 0
    table = review.read_text(encoding="utf-8")
    assert "| A01 | 1 | answerable |" in table
    assert "| N02 | 1 | not_in_docs |" in table
    assert "문서에 없다는 안내" in table


def test_signals_on_written_answers() -> None:
    qset = _qset()
    a01 = next(q for q in qset["questions"] if q["id"] == "A01")
    good = (
        "No. Under Article 4-3 of the Foreign Exchange Transaction Regulation (effective "
        "2026-11-02), the USD 100,000 rule does not apply to foreign residents. You need proof "
        "of how you earned the funds, and without it you can send up to USD 50,000 a year."
    )
    signals = REVIEW.signals(a01, {"answer": good, "tools": [], "numberCheck": {}})
    assert signals["factHints"] == [True, True, True]
    assert signals["citations"] >= 2 and signals["effectiveDate"] is True
    bad = REVIEW.signals(a01, {"answer": "Yes, you can.", "tools": [], "numberCheck": {}})
    assert bad["factHints"] == [False, False, False] and bad["citations"] == 0
    n01 = next(q for q in qset["questions"] if q["id"] == "N01")
    abstain = REVIEW.signals(n01, {"answer": "That fee is not in the documents I have."})
    assert abstain["saysNotInDocs"] is True


def test_review_rejects_unknown_questions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "other.jsonl"
    results.write_text(json.dumps({"type": "call", "questionId": "F01", "answer": ""}) + "\n")
    assert REVIEW.main([str(results), "--output", str(tmp_path / "r.md")]) == 1
    assert "F01" in capsys.readouterr().out

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
    assert len(questions) == 46
    counts = {
        c: sum(q["category"] == c for q in questions)
        for c in ("answerable", "not_in_docs", "out_of_scope")
    }
    assert counts == {"answerable": 32, "not_in_docs": 8, "out_of_scope": 6}
    assert len({q["id"] for q in questions}) == 46
    by_id = {q["id"]: q for q in questions}
    users = [q for q in questions if q["variant"] == "user"]
    assert len(users) == 16 and all(q["category"] == "answerable" for q in users)
    for q in users:
        # 사용자 변형: gold·핵심 사실·위험 오답이 원 문항과 같고, 조문 문구를 쓰지 않는다.
        base = by_id[q["variantOf"]]
        assert q["id"] == f"{base['id']}v" and base["variant"] == "original"
        for field in ("gold", "keyFacts", "riskyWrong", "queryKo", "queryEn"):
            assert q[field] == base[field]
        assert q["text"] != base["text"]
        assert "designated foreign exchange bank" not in q["text"].lower()
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
    assert signals["lawNames"] == 1 and signals["articles"] == 1
    assert signals["effectiveDate"] is True
    bad = REVIEW.signals(a01, {"answer": "Yes, you can.", "tools": [], "numberCheck": {}})
    assert bad["factHints"] == [False, False, False] and bad["lawNames"] == bad["articles"] == 0
    # 상담 전화번호는 근거 없는 숫자에서 뺀다. 단정 표현은 'against the law'·'prohibited'도 잡는다.
    row = {
        "answer": "Selling it is against the law and is prohibited. Call 1332.",
        "numberCheck": {"unsupported": ["1332", "5000"]},
        "tools": [
            {"toolName": "search_visa_policies", "arguments": {"query": "송금 한도"},
             "status": "FAILED", "errorCode": "SEARCH_NOT_CONFIGURED"}
        ],
        "usage": {"inputTokens": 10, "outputTokens": 2, "llmCalls": 2},
        "timings": {"firstDeltaMs": 5, "doneMs": 9},
    }  # fmt: skip
    s = REVIEW.signals(a01, row)
    assert s["unsupportedNumbers"] == ["5000"] and s["helplineNumbers"] == ["1332"]
    assert len(s["assertive"]) == 2
    assert s["queries"] == ["송금 한도"] and s["tools"][0]["errorCode"] == "SEARCH_NOT_CONFIGURED"
    assert (s["inputTokens"], s["llmCalls"], s["doneMs"]) == (10, 2, 9)
    n01 = next(q for q in qset["questions"] if q["id"] == "N01")
    abstain = REVIEW.signals(n01, {"answer": "That fee is not in the documents I have."})
    assert abstain["saysNotInDocs"] is True


def test_review_aggregates_grades_by_variant(tmp_path: Path) -> None:
    rows = [
        {"type": "call", "questionId": qid, "repeat": 1, "category": "answerable", "answer": "x"}
        for qid in ("A01", "A01v")
    ]
    results = tmp_path / "run.jsonl"
    results.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    grades = {
        "A01#1": {"grade": "abstain", "reason": "r", "abstainCause": "tool_failed"},
        "A01v#1": {"grade": "risky_wrong", "reason": "says yes", "outOfDocsClaims": 1},
    }
    grades_path = tmp_path / "grades.json"
    grades_path.write_text(json.dumps(grades), encoding="utf-8")
    review = tmp_path / "review.md"
    argv = [str(results), "--grades", str(grades_path), "--output", str(review)]
    assert REVIEW.main(argv) == 0
    table = review.read_text(encoding="utf-8")
    assert "| answerable | variant original | 0 | 0 | 0 | 0 | 1 | 0 | 0 |" in table
    assert "| answerable | variant user | 0 | 0 | 0 | 1 | 0 | 0 | 0 |" in table
    assert "| 문서 범위 밖 일반 진술 수 | 1 |" in table
    assert "tool_failed 1" in table
    grades_path.write_text(json.dumps({"A01#1": {"grade": "maybe"}}), encoding="utf-8")
    assert REVIEW.main(argv) == 1


def test_review_rejects_unknown_questions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "other.jsonl"
    results.write_text(json.dumps({"type": "call", "questionId": "F01", "answer": ""}) + "\n")
    assert REVIEW.main([str(results), "--output", str(tmp_path / "r.md")]) == 1
    assert "F01" in capsys.readouterr().out


def test_expected_tools_by_prompt_version() -> None:
    # v4.1 기준선은 규정 도구가 없고, v4.2(after)는 규정 질문에 search_finance_policy를 기대한다.
    by_id = {q["id"]: q for q in _qset()["questions"]}
    for qid in ("A01", "A01v", "N05"):
        assert EVAL.expected_tools(by_id[qid], "v4.1") == []
        assert EVAL.expected_tools(by_id[qid], "v4.2") == ["search_finance_policy"]
    assert EVAL.expected_tools(by_id["O02"], "v4.2") == ["recommend_savings_products"]


def test_policy_search_results_count_as_number_sources() -> None:
    # v4.2: 답변이 검색 결과의 '미화5만불'을 인용하면 근거 있는 숫자다(#88).
    metrics = {
        "tools": [
            {"toolName": "search_finance_policy", "query": "한도", "content": {"results": [
                {"document": "외국환거래규정", "ref": "제4-4조", "effectiveFrom": "2026-11-02",
                 "quote": "연간미화5만불범위내에서"}]}},
            {"toolName": "recommend_savings_products", "content": {}},
        ]
    }  # fmt: skip
    policy = EVAL.policy_tool_results(metrics)
    assert [t["toolName"] for t in policy] == ["search_finance_policy"]
    row: dict[str, Any] = {
        "answer": "Under the regulation you can send up to USD 50,000 a year.",
        "toolResult": {},
        "contextTexts": ["How much can I send?"],
        "judgmentFacts": False,
        "expectedTools": ["search_finance_policy"],
        "tools": [{"toolName": "search_finance_policy"}],
        "policyTools": policy,
    }
    EVAL.score(row)
    assert row["numberCheck"]["unsupported"] == []
    row["policyTools"] = []
    EVAL.score(row)
    assert row["numberCheck"]["unsupported"] == ["50,000"]


def test_external_policy_questions_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # hold-out처럼 레포 밖 파일: 경로로만 받고 내용은 출력하지 않는다.
    qset = _qset()
    secret = "HOLDOUT-ONLY question text"
    hold = {**qset, "questions": [{**qset["questions"][0], "id": "H01", "text": secret}]}
    path = tmp_path / "holdout.json"
    path.write_text(json.dumps(hold, ensure_ascii=False), encoding="utf-8")
    settings = _settings()
    output = tmp_path / "hold.jsonl"
    argv = ["--base-url", "http://testserver", "--policy-questions", str(path)]
    argv += ["--output", str(output)]
    code = EVAL.main(argv, client=_client(settings), settings=settings, confirm=lambda _: "n")
    assert code == 0
    conditions = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
    assert conditions["questionSet"]["external"] is True
    assert [r["questionId"] for r in REVIEW.load_rows(output)] == ["H01"]
    assert secret not in capsys.readouterr().out
    both = [*argv, "--product-questions", str(path)]
    with pytest.raises(SystemExit):
        EVAL.main(both, client=_client(settings), settings=settings, confirm=lambda _: "n")

"""scripts/compare_chat_versions.py: 두 프롬프트 설정 비교표(#101). API 호출 없음."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import compare_chat_versions as CMP  # noqa: E402

pytestmark = pytest.mark.unit


def _row(variant: str, qid: str, repeat: int, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "type": "call",
        "variant": variant,
        "questionId": qid,
        "category": "finance",
        "repeat": repeat,
        "warmup": False,
        "status": "done",
        "toolMatch": True,
        "numberCheck": {"unsupported": []},
        "tools": [{"toolName": "get_financial_analysis", "status": "SUCCEEDED"}],
        "expectedTools": ["get_financial_analysis"],
        "blocks": [],
        "answer": "ok",
        "timings": {"firstDeltaMs": 1000, "doneMs": 2000},
        "usage": {"inputTokens": 100, "outputTokens": 10},
        "costUsd": 0.001,
        "forbidden": {"hits": [], "negated": []},
    }
    row.update(extra)
    return row


def _write(path: Path, rows: list[dict[str, Any]]) -> Path:
    conditions = {
        "type": "conditions",
        "questionSet": {"file": "eval_questions_v1.json", "version": "v1.7"},
        "startedAt": "2026-11-19T00:00:00+00:00",
        "status": "completed",
        "questions": sorted({r["questionId"] for r in rows}),
        "repeats": 2,
        "spentUsd": 0.01,
        "variants": [
            {"name": "v4.2", "model": "m", "promptVersion": "v4.2"},
            {"name": "v4.4", "model": "m", "promptVersion": "v4.4"},
        ],
    }
    lines = [conditions, *rows]
    path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines), encoding="utf-8")
    return path


def test_regression_latency_and_failures(tmp_path: Path) -> None:
    rows = [
        _row("v4.2", "F01", 1),
        _row("v4.4", "F01", 1, timings={"firstDeltaMs": 1200, "doneMs": 2400}),
        _row("v4.2", "F01", 2),
        _row(
            "v4.4",
            "F01",
            2,
            toolMatch=False,
            tools=[],
            answer="I cannot check.",
            timings={"firstDeltaMs": 1200, "doneMs": 2400},
        ),
        # 오류 회차는 판정에서 뺀다.
        _row("v4.2", "F02", 1, status="error", errorCode="LLM_UPSTREAM_ERROR"),
        _row("v4.4", "F02", 1),
    ]
    path = _write(tmp_path / "eval.jsonl", rows)
    out = tmp_path / "cmp.md"
    assert CMP.main(["--base", "v4.2", "--new", "v4.4", "--output", str(out), str(path)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "| eval | F01 | tool | 2/2 → 1/2 |" in text
    # 도구를 하나도 부르지 않았으니 첫 도구도 틀렸다.
    assert "| eval | F01 | first | 2/2 → 1/2 |" in text
    assert "| F02 |" not in text.split("## 문항별")[0]  # base 오류만 있는 문항은 회귀 아님
    assert "| 전체 | 1000 | 1200 | +20.0% | 2000 | 2400 | +20.0% | 2 / 3 |" in text
    assert "**eval F01#2 v4.4** — tool, first" in text
    assert "> I cannot check." in text


def test_same_question_id_in_two_sets_is_kept_apart(tmp_path: Path) -> None:
    # eval O01(E-7-4)과 policy O01은 다른 문항이다. 한쪽 실패가 다른 쪽 회귀로 보이면 안 된다.
    eval_path = _write(tmp_path / "eval.jsonl", [_row("v4.2", "O01", 1), _row("v4.4", "O01", 1)])
    policy_path = _write(
        tmp_path / "policy.jsonl",
        [_row("v4.2", "O01", 1, toolMatch=False, tools=[]), _row("v4.4", "O01", 1)],
    )
    out = tmp_path / "cmp.md"
    argv = ["--base", "v4.2", "--new", "v4.4", "--output", str(out)]
    assert CMP.main([*argv, str(eval_path), str(policy_path)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "## 회귀(base 전 회차 통과 → new 실패)\n\n- 없음" in text
    assert "| eval | O01 | finance | 1/1 | 1/1 |" in text
    assert "| policy | O01 | finance | 0/1 | 1/1 |" in text


def test_first_tool_ignores_later_calls() -> None:
    row = _row(
        "v4.4",
        "N02",
        1,
        expectedTools=["search_finance_policy"],
        tools=[
            {"toolName": "search_finance_policy", "status": "FAILED"},
            {"toolName": "fx_rate_summary", "status": "SUCCEEDED"},
        ],
    )
    assert CMP.first_tool_ok(row) is True
    row["tools"] = row["tools"][::-1]
    assert CMP.first_tool_ok(row) is False
    assert CMP.first_tool_ok(_row("v4.4", "O04", 1, expectedTools=[], tools=[])) is True


def test_e74_rows_skip_number_check_and_check_card_values() -> None:
    card = {"type": "E74_SCORE_CARD", "total": 235, "required": 200}
    row = _row(
        "v4.4",
        "E01",
        1,
        tools=[{"toolName": "calculate_e74_score", "status": "SUCCEEDED"}],
        expectedTools=["calculate_e74_score"],
        numberCheck={"unsupported": ["47"]},
        blocks=[card],
        answer="Your score is 235 points, and 200 is required.",
    )
    assert CMP.number_ok(row) is None
    assert CMP.e74_card_in_answer(row) is True
    row["answer"] = "Your score is 235 points."
    assert CMP.e74_card_in_answer(row) is False
    assert CMP.failed(row) == ["e74CardValue"]


def test_language_section_and_language_failures(tmp_path: Path) -> None:
    korean = "현재 E-7-4 확정 점수는 172점입니다."
    rows = [
        # #106 전 측정 행(languageCheck·language 없음): 답변으로 다시 계산하고 en으로 본다.
        _row("v4.2", "E01", 1, answer=korean),
        _row("v4.4", "E01", 1, answer="Your score is 172 points."),
        _row(
            "v4.4",
            "E01",
            2,
            answer="Điểm của bạn là 172 điểm.",
            languageCheck={
                "language": "vi",
                "mixed": False,
                "mixedRaw": False,
                "mostlyKorean": False,
                "inLanguage": True,
                "hangulShare": 0.0,
                "viShare": 0.4,
                "disallowedRuns": [],
            },
        ),
    ]
    path = _write(tmp_path / "lang.jsonl", rows)
    out = tmp_path / "cmp.md"
    assert CMP.main(["--base", "v4.2", "--new", "v4.4", "--output", str(out), str(path)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "| v4.2 | en | 1 | 1/1 | 1/1 | 1/1 | 1/1 | 0/1 |" in text
    assert "| v4.4 | en | 1 | 0/1 | 0/1 | 0/1 | 0/1 | 1/1 |" in text
    assert "| v4.4 | vi | 1 | 0/1 | 0/1 | 0/1 | 0/1 | 1/1 |" in text
    assert CMP.failed(rows[0]) == ["language"]
    assert CMP.failed(rows[1]) == []

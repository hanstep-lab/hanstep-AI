"""scripts/evaluate_chat.py against the in-process app in fake mode (no network, no API)."""

from __future__ import annotations

import importlib.util
import json
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.integrations.policy_gateway import UnconfiguredPolicyGateway
from app.main import AppState, create_app
from app.rag.embeddings import BgeM3Embedder

pytestmark = pytest.mark.unit

TOKEN = "f" * 40
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _script() -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("evaluate_chat", SCRIPTS / "evaluate_chat.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EVAL = _script()


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "ai_internal_api_token": TOKEN,
        "chat_fake_delta_delay_ms": 0,
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def _client(settings: Settings) -> TestClient:
    app = create_app()
    app.state.rag = AppState(
        settings=settings, gateway=UnconfiguredPolicyGateway(), embedder=BgeM3Embedder()
    )
    return TestClient(app)


def _rows(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["type"] == "call"
    ]


def _run(settings: Settings, *argv: str, client: httpx.Client | None = None) -> int:
    code: int = EVAL.main(
        ["--base-url", "http://testserver", *argv],
        client=client or _client(settings),
        settings=settings,
        confirm=lambda _: "n",
    )
    return code


def test_fake_mode_two_questions(tmp_path: Path) -> None:
    settings = _settings()
    output = tmp_path / "run.jsonl"
    assert _run(settings, "--questions", "F01,T03", "--output", str(output)) == 0

    rows = _rows(output)
    assert [r["questionId"] for r in rows] == ["F01", "T03"]
    for row in rows:
        assert row["status"] == "done"
        assert row["costUsd"] == 0
        assert row["model"] == "fake-chat-model"
        assert row["toolMatch"] is True
        assert row["numberCheck"]["unsupported"] == []
        assert row["timings"]["firstDeltaMs"] <= row["timings"]["doneMs"]
        assert row["events"][-1] == "agent.done"
        assert "agent.metrics" in row["events"]
    summary = output.with_suffix(".md").read_text(encoding="utf-8")
    assert "server-default" in summary and "| F01 |" in summary
    assert "firstAnswerDeltaMs" in summary
    assert "도구 전 text가 나온 회차: 0/2" in summary


def test_variants_alternate_round_robin(tmp_path: Path) -> None:
    settings = _settings(chat_allow_config_override=True)
    variants = tmp_path / "variants.json"
    variants.write_text(
        json.dumps(
            [
                {"name": "sonnet", "model": "claude-sonnet-5-5", "thinking": "between_tools"},
                {
                    "name": "haiku",
                    "model": "claude-haiku-5-5",
                    "thinking": "disabled",
                    "effort": "low",
                },
            ]
        ),
        encoding="utf-8",
    )
    output = tmp_path / "run.jsonl"
    code = _run(
        settings,
        "--questions", "F01,F03", "--repeats", "2", "--warmup", "1",
        "--variants", str(variants), "--output", str(output),
    )  # fmt: skip
    assert code == 0
    rows = _rows(output)
    assert [(r["variant"], r["warmup"]) for r in rows[:2]] == [("sonnet", True), ("haiku", True)]
    measured = rows[2:]
    assert [r["variant"] for r in measured] == ["sonnet", "haiku"] * 4
    assert [r["questionId"] for r in measured] == ["F01"] * 4 + ["F03"] * 4
    assert [r["metrics"]["config"]["model"] for r in measured[:2]] == [
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
    ]


def test_output_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(_settings(), "--questions", "F01") == 2
    assert "--output is required" in capsys.readouterr().out


def test_max_usd_stops_before_a_paid_call(tmp_path: Path) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no call may be made over budget")

    settings = _settings(chat_llm_mode="anthropic", agent_model="claude-haiku-5-5")
    output = tmp_path / "run.jsonl"
    code = _run(
        settings,
        "--questions", "F01", "--llm-mode", "anthropic", "--max-usd", "0.0001", "--yes",
        "--output", str(output),
        client=httpx.Client(transport=httpx.MockTransport(refuse)),
    )  # fmt: skip
    assert code == 0
    assert _rows(output) == []
    conditions = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
    assert conditions["status"] == "budget_stopped"


def test_real_mode_asks_before_running(tmp_path: Path) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("declined run must not call")

    settings = _settings(chat_llm_mode="anthropic", agent_model="claude-haiku-5-5")
    code = _run(
        settings, "--questions", "F01", "--llm-mode", "anthropic",
        "--output", str(tmp_path / "x.jsonl"),
        client=httpx.Client(transport=httpx.MockTransport(refuse)),
    )  # fmt: skip
    assert code == 0
    assert not (tmp_path / "x.jsonl").exists()


def test_cost_uses_official_prices() -> None:
    usage = {"inputTokens": 1_000_000, "outputTokens": 1_000_000, "cacheReadInputTokens": 0,
             "cacheCreationInputTokens": 0}  # fmt: skip
    assert EVAL.cost_usd(usage, "claude-sonnet-5-5") == pytest.approx(12.0)
    assert EVAL.cost_usd(usage, "claude-haiku-5-5") == pytest.approx(0.6)
    assert EVAL.cost_usd(usage, "fake-chat-model") == 0


def test_rescore_without_calls(tmp_path: Path) -> None:
    settings = _settings()
    run = tmp_path / "run.jsonl"
    assert _run(settings, "--questions", "F01", "--output", str(run)) == 0
    lines = run.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[1])
    row["answer"] += " You send 29.8% of your income home."
    run.write_text("\n".join([lines[0], json.dumps(row)]) + "\n", encoding="utf-8")

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("rescore must not call")

    rescored = tmp_path / "rescored.jsonl"
    code = EVAL.main(
        ["--rescore", str(run), "--output", str(rescored)],
        client=httpx.Client(transport=httpx.MockTransport(refuse)),
    )
    assert code == 0
    assert _rows(rescored)[0]["numberCheck"]["unsupported"] == ["29.8%"]
    assert "29.8%" in rescored.with_suffix(".md").read_text(encoding="utf-8")


def _sse_server(*events: tuple[str, dict[str, Any]]) -> httpx.Client:
    """A server that answers every INT-05 call with the given SSE events (no app, no API)."""
    body = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _call(model: str, stop: str, tokens_in: int, tokens_out: int) -> dict[str, Any]:
    return {"model": model, "stopReason": stop, "inputTokens": tokens_in,
            "outputTokens": tokens_out, "cacheReadInputTokens": 0,
            "cacheCreationInputTokens": 0, "thinkingBlocks": 1, "firstTextMs": None,
            "durationMs": 900}  # fmt: skip


EMPTY_ANSWER_RUN = (
    (
        "agent.metrics",
        {
            "config": {"model": "claude-haiku-5-5"},
            "calls": [
                _call("claude-haiku-5-5", "tool_use", 1000, 100),
                _call("claude-haiku-5-5", "max_tokens", 1500, 2000),
            ],
            "firstAnswerDeltaMs": None,
            "preToolTextChars": 0,
        },
    ),
    ("agent.error", {"code": "AGENT_INTERNAL_ERROR", "message": "Something went wrong."}),
)


def test_error_run_cost_counts_toward_budget_and_max_tokens(tmp_path: Path) -> None:
    output = tmp_path / "run.jsonl"
    # Per error run: (2500 x 0.10 + 2100 x 0.50) / 1M = $0.0013. Budget $0.003 allows two.
    code = _run(
        _settings(agent_model="claude-haiku-5-5"), "--questions", "F01", "--repeats", "3",
        "--llm-mode", "anthropic", "--yes",
        "--max-usd", "0.003", "--output", str(output), client=_sse_server(*EMPTY_ANSWER_RUN),
    )  # fmt: skip
    assert code == 0
    rows = _rows(output)
    assert len(rows) == 2
    for row in rows:
        assert row["status"] == "error"
        assert row["usage"]["outputTokens"] == 2100
        assert row["costUsd"] == pytest.approx(0.0013)
    conditions = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
    assert conditions["status"] == "budget_stopped"
    assert conditions["spentUsd"] == pytest.approx(0.0026)
    summary = output.with_suffix(".md").read_text(encoding="utf-8")
    assert "stop_reason=max_tokens: 2" in summary


@pytest.mark.parametrize(
    "events",
    [
        EMPTY_ANSWER_RUN,
        (
            (
                "agent.done",
                {
                    "content": "ok",
                    "modelId": "fake-chat-model",
                    "timings": {},
                    "usage": {
                        "inputTokens": 10,
                        "outputTokens": 5,
                        "cacheReadInputTokens": 0,
                        "cacheCreationInputTokens": 0,
                        "llmCalls": 1,
                    },
                },
            ),
        ),  # fmt: skip
    ],
    ids=["real-model", "real-tokens"],
)
def test_fake_mode_stops_when_server_is_real(
    tmp_path: Path, events: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "run.jsonl"
    code = _run(
        _settings(), "--questions", "F01,F03", "--output", str(output),
        client=_sse_server(*events),
    )  # fmt: skip
    assert code == 3
    assert len(_rows(output)) == 1
    conditions = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
    assert conditions["status"] == "mode_mismatch"
    assert "서버가 실제 모드" in capsys.readouterr().out


CHAT_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "chat" / "finance_context_minh.json"
)


def _fixture_value(name: str) -> Decimal:
    if name.isdigit():
        return Decimal(name)
    node: Any = json.loads(CHAT_FIXTURE.read_text(encoding="utf-8"))["latestAnalysis"]
    for part in name.split("."):
        node = node[part]
    return Decimal(str(node["amount"]))


def test_reference_values_match_fixture() -> None:
    """Hand-written referenceValues equal the formula computed from the chat fixture."""
    spec = EVAL.load_questions()
    assert spec["version"] == "v1.1"
    assert spec["latencySubset"] == ["F01", "F03", "F07", "F09", "H01"]
    with_refs = [q for q in spec["questions"] if q.get("referenceValues")]
    assert [q["id"] for q in with_refs] == ["T02", "T03"] + [f"C0{i}" for i in range(1, 8)]
    for q in with_refs:
        assert q["expectBehavior"] == "calculation_needed"
        for ref in q["referenceValues"]:
            left, op, right = ref["formula"].split()
            a, b = _fixture_value(left), _fixture_value(right)
            value = {"*": a * b, "-": a - b, "/": a / b}[op]
            if ref["unit"] == "percent":
                value = (value * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
            assert value == Decimal(str(ref["value"])), (q["id"], ref)


def _calc_row(question_id: str, answer: str) -> dict[str, Any]:
    question = next(q for q in EVAL.load_questions()["questions"] if q["id"] == question_id)
    row: dict[str, Any] = {
        "answer": answer,
        "toolResult": EVAL.tool_context(_settings(), 1, "fake"),
        "contextTexts": [question["text"]],
        "expectedTools": question["expectedTools"],
        "referenceValues": question["referenceValues"],
        "tools": [],
    }
    EVAL.score(row)
    return row


@pytest.mark.parametrize(
    ("question_id", "answer", "verdict"),
    [
        ("C01", "In 6 months you would have 4,051,902 KRW.", "reference"),
        ("C01", "That is about 4,050,000 won.", "reference"),
        ("C01", "That is about 4,052,000 won.", "reference"),
        ("T02", "You would have about 8.1 million won in a year.", "reference"),
        ("T03", "You send 29.8% of your income home.", "reference"),
        ("T03", "You send about 30% of your income home.", "reference"),
        ("C02", "About 22.1 percent of your income goes to fixed living costs.", "reference"),
        ("C01", "You would have 5,000,000 won.", "wrong_number"),
        ("T03", "You send about 31% of your income home.", "wrong_number"),
        (
            "C01",
            "Your spare money is KRW 675,317 a month. I cannot calculate totals yet.",
            "no_value",
        ),
        ("C01", "I cannot calculate that for you yet.", "no_value"),
    ],  # fmt: skip
)
def test_reference_check(question_id: str, answer: str, verdict: str) -> None:
    check = _calc_row(question_id, answer)["referenceCheck"]
    assert check["verdict"] == verdict, check


def test_reference_check_percent_does_not_match_amount() -> None:
    # A percent reference needs a percent in the answer: 29.8 without % does not count.
    assert _calc_row("T03", "That is 29.8 of it.")["referenceCheck"]["verdict"] != "reference"


def test_delta_times_question_set_and_reference_summary(tmp_path: Path) -> None:
    output = tmp_path / "run.jsonl"
    assert _run(_settings(), "--questions", "C01,T03", "--output", str(output)) == 0

    lines = output.read_text(encoding="utf-8").splitlines()
    conditions = json.loads(lines[0])
    assert conditions["questionSet"] == {"file": "eval_questions_v1.json", "version": "v1.1"}
    rows = _rows(output)
    for row in rows:
        assert len(row["deltaTimesMs"]) == row["events"].count("agent.delta") > 0
        assert row["deltaTimesMs"] == sorted(row["deltaTimesMs"])
        assert row["deltaTimesMs"][0] == row["timings"]["firstDeltaMs"]
        assert row["referenceCheck"]["verdict"] in ("reference", "wrong_number", "no_value")
    summary = output.with_suffix(".md").read_text(encoding="utf-8")
    assert "- 질문 세트: eval_questions_v1.json (v1.1)" in summary
    assert "- 계산 질문 2개: 참조값 포함 " in summary

    # --rescore recomputes the reference check from the saved answer.
    saved = [json.loads(line) for line in lines[1:]]
    for row in saved:
        if row["questionId"] == "T03":
            row["answer"] = "You send 29.8% of your income home."
    output.write_text(
        "\n".join([lines[0], *(json.dumps(r) for r in saved)]) + "\n", encoding="utf-8"
    )
    rescored = tmp_path / "rescored.jsonl"
    assert EVAL.main(["--rescore", str(output), "--output", str(rescored)]) == 0
    t03 = next(r for r in _rows(rescored) if r["questionId"] == "T03")
    assert t03["referenceCheck"]["verdict"] == "reference"
    assert "  - T03 #1: 참조값 포함 (29.8%)" in rescored.with_suffix(".md").read_text(
        encoding="utf-8"
    )

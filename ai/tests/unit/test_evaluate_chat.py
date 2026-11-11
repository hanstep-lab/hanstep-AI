"""scripts/evaluate_chat.py against the in-process app in fake mode (no network, no API)."""

from __future__ import annotations

import importlib.util
import json
import sys
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
        "--questions", "F01", "--max-usd", "0.0001", "--yes", "--output", str(output),
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
        settings, "--questions", "F01", "--output", str(tmp_path / "x.jsonl"),
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

"""scripts/count_chat_tokens.py with a fake counter (count_tokens is never called for real)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _script() -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "count_chat_tokens", SCRIPTS / "count_chat_tokens.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


COUNT = _script()


class FakeCounter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(count_tokens=self._count)

    def _count(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(input_tokens=len(json.dumps(kwargs, ensure_ascii=False)) // 4)


def test_requires_output(capsys: pytest.CaptureFixture[str]) -> None:
    assert COUNT.main([], client=FakeCounter()) == 2
    assert "--output is required" in capsys.readouterr().out


def test_counts_both_approaches_per_finance_question(tmp_path: Path) -> None:
    counter = FakeCounter()
    output = tmp_path / "tokens.md"
    assert (
        COUNT.main(["--models", "claude-haiku-5-5", "--output", str(output)], client=counter) == 0
    )

    report = output.read_text(encoding="utf-8")
    assert "count_tokens 는 추정치" in report
    assert report.count("| claude-haiku-5-5 | F") == 11
    raw = next(c for c in counter.calls if "tools" not in c and "transactions" in str(c))
    assert raw["messages"][0]["content"].count('"id"') == 341
    second = next(c for c in counter.calls if "tools" in c and len(c["messages"]) == 3)
    assert second["messages"][2]["content"][0]["type"] == "tool_result"
    assert "675317" in second["messages"][2]["content"][0]["content"]

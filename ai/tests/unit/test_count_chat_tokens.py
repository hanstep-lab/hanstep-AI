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


def test_product_baseline_counts_v3_tools_per_question(tmp_path: Path) -> None:
    counter = FakeCounter()
    output = tmp_path / "product.md"
    argv = ["--question-set", "product", "--models", "claude-haiku-5-5", "--output", str(output)]
    assert COUNT.main(argv, client=counter) == 0

    report = output.read_text(encoding="utf-8")
    assert report.count("| claude-haiku-5-5 | Q") == 32
    assert "| claude-haiku-5-5 | 평균 |" in report
    names = [t["name"] for t in counter.calls[0]["tools"]]
    assert names[0] == "get_financial_analysis"
    assert "recommend_savings_products" not in names  # 기준선: 상품 도구 없음
    assert len(counter.calls) == 64  # 1차·2차 × 32문항


def test_policy_baseline_counts_v4_1_tools_per_question(tmp_path: Path) -> None:
    # 금융 정책 RAG(#88) 기준선: 기본 프롬프트 v4.1 + v4.1 도구(규정 도구 없음).
    counter = FakeCounter()
    output = tmp_path / "policy.md"
    argv = ["--question-set", "policy", "--models", "claude-haiku-5-5", "--output", str(output)]
    assert COUNT.main(argv, client=counter) == 0

    report = output.read_text(encoding="utf-8")
    assert "chat-v4.1" in report and "규정 도구 없음" in report
    assert sum(report.count(f"| claude-haiku-5-5 | {p}") for p in "ANO") == 46
    names = [t["name"] for t in counter.calls[0]["tools"]]
    assert names[0] == "get_financial_analysis" and names[-1] == "recommend_savings_products"
    assert counter.calls[0]["system"] == COUNT.system_prompt("en", "v4.1")
    assert len(counter.calls) == 92  # 1차·2차 × 46문항


def test_policy_v4_2_adds_the_policy_tool_and_search_result(tmp_path: Path) -> None:
    counter = FakeCounter()
    output = tmp_path / "policy42.md"
    argv = ["--question-set", "policy", "--prompt-version", "v4.2", "--models", "claude-haiku-5-5"]
    assert COUNT.main([*argv, "--output", str(output)], client=counter) == 0
    assert "chat-v4.2" in output.read_text(encoding="utf-8")
    names = [t["name"] for t in counter.calls[0]["tools"]]
    assert names[-1] == "search_finance_policy"
    assert counter.calls[0]["system"] == COUNT.system_prompt("en", "v4.2")
    second = next(c for c in counter.calls if len(c["messages"]) == 3)
    use = second["messages"][1]["content"][0]
    assert use["name"] == "search_finance_policy" and use["input"]["queryKo"]
    assert "effectiveFrom" in second["messages"][2]["content"][0]["content"]


def test_policy_v4_3a_counts_with_the_same_tools_as_v4_2(tmp_path: Path) -> None:
    # 스펙 가드 ①(#88): v4.3a는 프롬프트 한 줄만 다르고 도구·검색 결과 예시는 v4.2와 같다.
    counter = FakeCounter()
    output = tmp_path / "policy43a.md"
    argv = ["--question-set", "policy", "--prompt-version", "v4.3a", "--models", "claude-haiku-5-5"]
    assert COUNT.main([*argv, "--output", str(output)], client=counter) == 0
    assert "chat-v4.3a" in output.read_text(encoding="utf-8")
    assert counter.calls[0]["system"] == COUNT.system_prompt("en", "v4.3a")
    assert [t["name"] for t in counter.calls[0]["tools"]][-1] == "search_finance_policy"


def test_system_mode_counts_system_prompt_per_version_nationality_and_turn(tmp_path: Path) -> None:
    # #130: 시스템 프롬프트만의 토큰(system 있는 호출 - 없는 호출). 도구는 넣지 않는다.
    counter = FakeCounter()
    output = tmp_path / "system.md"
    argv = [
        "--question-set", "system", "--versions", "v4.7,v4.8", "--languages", "en",
        "--nationalities", "none,PHL", "--turns", "first,later", "--models", "claude-haiku-5-5",
        "--output", str(output),
    ]  # fmt: skip
    assert COUNT.main(argv, client=counter) == 0
    rows = [line for line in output.read_text(encoding="utf-8").splitlines() if "claude" in line]
    assert len(rows) == 8
    assert all("tools" not in c for c in counter.calls)

    def tokens(prompt: str, nationality: str, turn: str) -> int:
        row = next(r for r in rows if f"| {prompt} | en | {nationality} | {turn} |" in r)
        return int(row.rsplit("|", 2)[1])

    assert tokens("v4.8", "-", "first") == tokens("v4.7", "-", "first")
    assert tokens("v4.8", "PHL", "first") > tokens("v4.7", "PHL", "first")
    assert tokens("v4.7", "PHL", "first") == tokens("v4.7", "-", "first")


def test_system_mode_rejects_unknown_versions_and_turns(tmp_path: Path) -> None:
    output = str(tmp_path / "x.md")
    with pytest.raises(SystemExit):
        COUNT.main(["--question-set", "system", "--versions", "v9", "--output", output],
                   client=FakeCounter())  # fmt: skip
    with pytest.raises(SystemExit):
        COUNT.main(["--question-set", "system", "--turns", "second", "--output", output],
                   client=FakeCounter())  # fmt: skip

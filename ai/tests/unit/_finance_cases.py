"""tests/fixtures/finance/ 아래에 있는, 손으로 계산한 금융 시나리오를 읽어 온다."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "finance"
SCENARIOS = FIXTURES / "scenarios"


def load_scenarios() -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(SCENARIOS.glob("S*.json"))]


def ready_scenarios() -> list[dict[str, Any]]:
    return [s for s in load_scenarios() if s["status"] == "ready"]


def ready_ok_scenarios() -> list[dict[str, Any]]:
    """200 응답을 기대하는 ready 시나리오(400 시나리오는 제외)."""
    return [s for s in ready_scenarios() if s["expected"].get("status") != 400]


def ready_error_scenarios() -> list[dict[str, Any]]:
    return [s for s in ready_scenarios() if s["expected"].get("status") == 400]


def load_fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data

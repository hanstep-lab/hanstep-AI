"""Chat tool ``get_financial_analysis``: Spring INT-06 summary for the requesting user only.

The tool takes no arguments. The user id is fixed per request when the tool is built, so the
LLM has no way to name another user. Only the values the answer needs go to the LLM: no ids,
no isMock, no accounts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from app.integrations.spring_client import SpringApiError, SpringInternalClient

logger = logging.getLogger(__name__)

TOOL_NAME = "get_financial_analysis"
# Anthropic tool format; no input properties on purpose.
TOOL_SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Get the current user's latest monthly cash-flow analysis (income, fixed living costs, "
        "remittance, variable spending, spare money), the commitments they declared during "
        "onboarding and their savings goals. Amounts are KRW integers. Takes no arguments."
    ),
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
}
UNAVAILABLE = "data unavailable"
NO_ANALYSIS = {"analysis": None, "message": "No financial analysis result."}
# Test/fake-mode INT-06 response (demo values, not real user data).
FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "chat"
    / "finance_context_minh.json"
)


class FinanceContextSource(Protocol):
    async def fetch(self, user_id: int, request_id: str | None) -> dict[str, Any]: ...


class FixtureFinanceContext:
    """fake mode or Spring not configured: the Minh fixture, whatever the user id."""

    def __init__(self, path: Path = FIXTURE) -> None:
        self._path = path

    async def fetch(self, user_id: int, request_id: str | None) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self._path.read_text(encoding="utf-8"))
        data["userId"] = user_id
        return data


class SpringFinanceContext:
    """anthropic mode with Spring configured: real INT-06 call."""

    def __init__(self, client: SpringInternalClient, timeout: float) -> None:
        self._client = client
        self._timeout = timeout

    async def fetch(self, user_id: int, request_id: str | None) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._client.finance_context, user_id, timeout=self._timeout, request_id=request_id
        )


@dataclass
class ToolOutcome:
    name: str
    arguments: dict[str, Any]
    status: str  # SUCCEEDED | FAILED | TIMEOUT | REJECTED
    content: str  # what the LLM sees
    latency_ms: int
    result_ref: dict[str, Any] | None = None
    error_code: str | None = None
    sources: list[dict[str, Any]] = field(default_factory=list)


def _amount(money: Any) -> int | None:
    return int(money["amount"]) if isinstance(money, dict) and "amount" in money else None


def summarize(context: dict[str, Any]) -> dict[str, Any]:
    """INT-06 response -> compact JSON for the LLM. KeyError/TypeError on a malformed body."""
    analysis = context["latestAnalysis"]
    variable = analysis["variableExpense"] or {}
    quality = analysis["dataQuality"] or {}
    commitments = context.get("commitments") or {}
    return {
        "currency": "KRW",
        "analysis": {
            "periodFrom": analysis["periodFrom"],
            "periodTo": analysis["periodTo"],
            "avgMonthlyIncome": _amount(analysis["avgMonthlyIncome"]),
            "fixedLiving": _amount(analysis["fixedLiving"]),
            "remittance": _amount(analysis["remittance"]),
            "variableExpense": {
                "avg": _amount(variable.get("avg")),
                "min": _amount(variable.get("min")),
                "max": _amount(variable.get("max")),
            },
            "spare": _amount(analysis["spare"]),
            "observedMonthCount": quality.get("observedMonthCount"),
            "warnings": quality.get("warnings") or [],
        },
        # Entered by the user during onboarding, not computed from transactions.
        "declaredCommitments": {
            key: _amount(commitments.get(key))
            for key in (
                "fixedLiving",
                "remittance",
                "variableExpense",
                "mandatorySaving",
                "deductibleTotal",
            )
        },
        "goals": [
            {
                "goalType": goal.get("goalType"),
                "targetAmount": _amount(goal.get("targetAmount")),
                "targetDate": goal.get("targetDate"),
                "status": goal.get("status"),
                "monthlyRequired": _amount(goal.get("monthlyRequired")),
            }
            for goal in context.get("goals") or []
        ],
    }


class FinanceTool:
    """Bound to one request: user id and request id cannot be changed by the LLM."""

    def __init__(
        self,
        source: FinanceContextSource,
        *,
        user_id: int,
        request_id: str | None,
        timeout: float,
    ) -> None:
        self._source = source
        self._user_id = user_id
        self._request_id = request_id
        self._timeout = timeout

    async def run(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        started = time.perf_counter()

        def outcome(status: str, content: Any, **extra: Any) -> ToolOutcome:
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            latency = round((time.perf_counter() - started) * 1000)
            return ToolOutcome(name, arguments, status, text, latency, **extra)

        if name != TOOL_NAME:
            return outcome("REJECTED", "unknown tool", error_code="UNKNOWN_TOOL")
        try:
            context = await asyncio.wait_for(
                self._source.fetch(self._user_id, self._request_id), self._timeout
            )
        except (TimeoutError, httpx.TimeoutException):
            return outcome("TIMEOUT", UNAVAILABLE, error_code="SPRING_TIMEOUT")
        except SpringApiError as exc:
            return outcome("FAILED", UNAVAILABLE, error_code=f"SPRING_HTTP_{exc.status}")
        except Exception as exc:  # transport and anything else: answer without the data
            logger.warning("finance context failed: %s", type(exc).__name__)
            return outcome("FAILED", UNAVAILABLE, error_code="SPRING_ERROR")

        analysis = context.get("latestAnalysis")
        if analysis is None:
            return outcome("SUCCEEDED", NO_ANALYSIS)
        try:
            summary = summarize(context)
            ref = {"type": "FINANCIAL_ANALYSIS", "id": int(analysis["id"])}
        except (KeyError, TypeError, ValueError):
            return outcome("FAILED", UNAVAILABLE, error_code="INVALID_CONTEXT")
        source = {**ref, "chunkRef": None, "pageLabel": None}
        return outcome("SUCCEEDED", summary, result_ref=ref, sources=[source])

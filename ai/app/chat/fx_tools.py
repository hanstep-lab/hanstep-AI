"""챗봇 환율 진단 도구 fx_rate_summary(#91). Spring hanstep-backend#127의 내부 API를 부른다.

- Spring GET /internal/v1/ai-context/users/{userId}/fx-summary?currency= :
  사용자 화면 통화(USD + 모국 통화)의 현재 일별 고시 환율과 지난 1·3·6·12개월 평균 비교.
  숫자는 Spring(환율 화면과 같은 계산)만 만든다.
- 사용자 id와 요청 id는 요청마다 고정된다. LLM 인자는 currency(선택) 하나라
  다른 사용자를 지정할 수 없다.
- LLM에는 값만 준다(문장 없음). 과거 평균과의 비교만 있고 예측·권유는 없다.
- isMock·stale은 LLM에 준다. 프롬프트 규칙이 "isMock이면 데모 데이터임을 밝힌다",
  "stale이면 기준 시각을 강조한다"이기 때문이다(송금 도구 get_fx_rate는 isMock을 뺀다).
- 측정용(agent.metrics의 tools)으로 LLM에 준 값을 measurement.content에 남긴다.
  숫자 검사가 이 값을 근거로 쓴다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.chat.tools import UNAVAILABLE, ToolOutcome
from app.integrations.spring_client import SpringApiError, SpringInternalClient

logger = logging.getLogger(__name__)

FX_RATE_SUMMARY = "fx_rate_summary"
QUOTE_CURRENCY = "KRW"
# 기간 키 → 개월 수(LLM이 "3-month average"처럼 말할 때 그 숫자의 근거).
PERIOD_MONTHS = {"1M": 1, "3M": 3, "6M": 6, "1Y": 12}
NO_DATA = "no exchange rate data for this currency"
NOT_USER_CURRENCY = (
    "this currency is not one of the user's currencies; call again without currency to get "
    "all of the user's currencies"
)
# 테스트·fake 모드·Spring 미설정 시 쓰는 값.
# 2026-11-18 로컬 Reyes 실제 출력(USD는 로컬 Mock 시드라 isMock).
FIXTURE = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "chat" / "fx_summary_reyes.json"
)

TOOL_SCHEMA: dict[str, Any] = {
    "name": FX_RATE_SUMMARY,
    "description": (
        "Get the current user's exchange rate summary for their currencies (USD and their home "
        "currency): the latest daily rate in KRW per `unit` of the currency, its source, the "
        "as-of date and the last check time, the change from the previous business day, and how "
        "the rate compares with the averages of the last 1, 3, 6 and 12 months (difference, "
        "direction and the number of business days in each average). Past comparison only: it "
        "does not forecast rates. Omit currency to get all of the user's currencies."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "currency": {
                "type": "string",
                "pattern": "^[A-Z]{3}$",
                "description": "Optional ISO 4217 code, e.g. PHP or USD",
            }
        },
        "additionalProperties": False,
    },
}


class FxSummaryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")

    @field_validator("currency", mode="before")
    @classmethod
    def _upper(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value


class FxSummarySource(Protocol):
    async def summary(
        self, user_id: int, currency: str | None, request_id: str | None
    ) -> dict[str, Any]: ...


class FixtureFxSummary:
    """fake 모드이거나 Spring 설정이 없을 때: Reyes 실제 출력 고정값(사용자 id와 상관없이)."""

    def __init__(self, path: Path = FIXTURE) -> None:
        self._data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))

    async def summary(
        self, user_id: int, currency: str | None, request_id: str | None
    ) -> dict[str, Any]:
        data = {**self._data, "userId": user_id}
        if currency is None:
            return data
        if currency not in data["screenCurrencies"]:
            raise SpringApiError(400, "VALIDATION_ERROR", "")
        found = [c for c in data["currencies"] if c["currency"] == currency]
        if not found:
            raise SpringApiError(404, "FX_RATE_NOT_FOUND", "")
        return {**data, "currencies": found, "unavailableCurrencies": []}


class SpringFxSummary:
    """anthropic 모드 + Spring 설정: Spring 내부 API(fx-summary)."""

    def __init__(self, client: SpringInternalClient, timeout: float) -> None:
        self._client = client
        self._timeout = timeout

    async def summary(
        self, user_id: int, currency: str | None, request_id: str | None
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._client.fx_summary,
            user_id,
            currency=currency,
            timeout=self._timeout,
            request_id=request_id,
        )


def summary_for_llm(data: dict[str, Any]) -> dict[str, Any]:
    """Spring fx-summary 응답 → LLM에 줄 값(문장 없음). 본문이 계약과 다르면 KeyError·TypeError."""
    currencies = []
    for item in data["currencies"]:
        averages: list[dict[str, Any]] = []
        insufficient: list[str] = []
        for period in item["periodAverages"]:
            if period["avg"] is None:
                insufficient.append(period["period"])
                continue
            averages.append(
                {
                    "period": period["period"],
                    "months": PERIOD_MONTHS[period["period"]],
                    "average": period["avg"],
                    "difference": period["diff"],
                    "differencePct": period["diffRate"],
                    # 현재 값이 그 기간 평균보다 LOWER·HIGHER, ±0.10% 이내면 SAME
                    "currentVsAverage": period["direction"],
                    "businessDays": period["businessDays"],
                }
            )
        currencies.append(
            {
                "currency": item["currency"],
                "unit": item["unit"],
                "quoteCurrency": QUOTE_CURRENCY,
                "rate": item["rate"],
                "source": item["source"],
                "asOf": item["asOf"],
                "lastCheckedAt": item["fetchedAt"],
                "changeFromPreviousBusinessDay": item["change"],
                "changePct": item["changeRate"],
                "periodAverages": averages,
                "insufficientDataPeriods": insufficient,
                "isMock": bool(item["isMock"]),
                "stale": bool(item["stale"]),
            }
        )
    return {
        "currencies": currencies,
        "unavailableCurrencies": list(data.get("unavailableCurrencies") or []),
    }


class FxTools:
    """요청 하나에 묶인 환율 진단 도구."""

    def __init__(
        self,
        source: FxSummarySource,
        *,
        user_id: int,
        request_id: str | None,
        timeout: float,
    ) -> None:
        self._source = source
        self._user_id = user_id
        self._request_id = request_id
        self._timeout = timeout

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [dict(TOOL_SCHEMA)]

    async def run(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        started = time.perf_counter()

        def outcome(status: str, content: Any, **extra: Any) -> ToolOutcome:
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            latency = round((time.perf_counter() - started) * 1000)
            return ToolOutcome(name, arguments, status, text, latency, **extra)

        if name != FX_RATE_SUMMARY:
            return outcome("REJECTED", "unknown tool", error_code="UNKNOWN_TOOL")
        try:
            parsed = FxSummaryInput.model_validate(arguments)
        except ValidationError as exc:
            fields = [".".join(str(p) for p in e["loc"]) + f" ({e['type']})" for e in exc.errors()]
            return outcome(
                "REJECTED",
                f"invalid arguments: {', '.join(fields)}",
                error_code="INVALID_ARGUMENTS",
            )
        try:
            data = await asyncio.wait_for(
                self._source.summary(self._user_id, parsed.currency, self._request_id),
                self._timeout,
            )
        except (TimeoutError, httpx.TimeoutException):
            return outcome("TIMEOUT", UNAVAILABLE, error_code="SPRING_TIMEOUT")
        except SpringApiError as exc:
            error_code = f"SPRING_HTTP_{exc.status}"
            if exc.status == 400 and exc.code == "VALIDATION_ERROR":
                # 화면 통화(USD + 모국 통화)가 아님: currency 없이 다시 부르면 쓸 수 있는
                # 통화가 나온다.
                return outcome("REJECTED", NOT_USER_CURRENCY, error_code=error_code)
            if exc.code == "FX_RATE_NOT_FOUND":
                return outcome("FAILED", NO_DATA, error_code=error_code)
            return outcome("FAILED", UNAVAILABLE, error_code=error_code)
        except Exception as exc:  # 연결 실패 등: 환율 없이 답하게 한다
            logger.warning("fx summary failed: %s", type(exc).__name__)
            return outcome("FAILED", UNAVAILABLE, error_code="SPRING_ERROR")
        try:
            content = summary_for_llm(data)
        except (KeyError, TypeError, ValueError):
            return outcome("FAILED", UNAVAILABLE, error_code="INVALID_RESPONSE")
        return outcome(
            "SUCCEEDED",
            content,
            measurement={"arguments": arguments, "content": content},
        )

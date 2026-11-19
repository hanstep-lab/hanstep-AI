"""챗봇 환율 진단 도구 fx_rate_summary(#91). Spring hanstep-backend#127의 내부 API를 부른다.

형식(인자·출력·오류·프롬프트 규칙)은 specs/chat/fx_summary_tool_v1.md에 정리했다.

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
- 성공하면 환율 진단 카드(FX_SUMMARY_CARD, agent.done의 blocks)를 낸다(#95). 카드는 LLM용
  결과가 아니라 Spring 원본에서 만든다(송금 환율 sendRate·receiveRate는 LLM에 주지 않는다).
  필드 이름은 환율 화면 API(FX-001)와 같다. 버튼(환율 상세 보기 등)은 앱이 고정으로 붙인다.
- 한 턴에 여러 번 불러도 카드는 하나다: 스트림이 merge_fx_card로 통화 기준으로 합친다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    ValidationError,
    field_validator,
)

from app.chat.remittance_tools import Contract, CurrencyCode, Rate
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


# --- 환율 진단 카드(FX_SUMMARY_CARD, #95) ------------------------------------------------

FX_SUMMARY_CARD = "FX_SUMMARY_CARD"
# 부호가 있는 값(전일 대비, 평균과의 차이). Decimal로 받고 JSON에는 숫자로 낸다.
Signed = Annotated[Decimal, PlainSerializer(float, return_type=float, when_used="json")]


class FxPeriodAverage(Contract):
    """평균을 낸 기간 하나(FX-001 periodAverages와 같은 이름). 데이터 부족 기간은 카드에 없다."""

    period: Literal["1M", "3M", "6M", "1Y"]
    avg: Rate
    diff: Signed = Field(description="현재 − 평균")
    diff_rate: Signed = Field(description="(현재 − 평균) ÷ 평균 × 100(%)")
    direction: Literal["LOWER", "HIGHER", "SAME"]
    business_days: int = Field(ge=1)


class FxCurrencyCard(Contract):
    """통화 하나. ``unit`` 외화 = ``rate`` KRW(FX-001과 같은 이름)."""

    currency: CurrencyCode
    unit: int = Field(ge=1)
    rate: Rate
    send_rate: Rate | None = Field(description="송금 보낼 때. KOREAEXIM만 있고 그 밖에는 null")
    receive_rate: Rate | None = Field(description="송금 받을 때. KOREAEXIM만 있고 그 밖에는 null")
    change: Signed | None = Field(description="전 영업일 대비. 전 영업일 값이 없으면 null")
    change_rate: Signed | None = Field(description="전 영업일 대비(%). 없으면 null")
    source: str = Field(min_length=1, max_length=30)
    as_of: date
    fetched_at: datetime
    period_averages: tuple[FxPeriodAverage, ...]
    is_mock: bool
    stale: bool


class FxSummaryCardBlock(Contract):
    """환율 진단 카드(chat_messages.blocks_json). 문장·버튼 경로는 넣지 않는다."""

    type: Literal["FX_SUMMARY_CARD"] = "FX_SUMMARY_CARD"
    currencies: tuple[FxCurrencyCard, ...] = Field(min_length=1)
    unavailable_currencies: tuple[CurrencyCode, ...]


def summary_block(data: dict[str, Any]) -> dict[str, Any] | None:
    """Spring fx-summary 원본 → 카드. 평균을 내지 않은 기간(avg null)은 뺀다. 통화가 없으면 None."""
    if not data["currencies"]:
        return None
    currencies = [
        {**item, "periodAverages": [p for p in item["periodAverages"] if p["avg"] is not None]}
        for item in data["currencies"]
    ]
    block = FxSummaryCardBlock.model_validate(
        {
            "currencies": currencies,
            "unavailableCurrencies": list(data.get("unavailableCurrencies") or []),
        }
    )
    result: dict[str, Any] = block.model_dump(mode="json", by_alias=True)
    return result


def merge_fx_card(blocks: list[dict[str, Any]], card: dict[str, Any]) -> None:
    """한 턴의 카드를 하나로 합친다(blocks를 바꾼다).

    - 카드 자리는 처음 나온 FX 카드 자리. 통화 순서는 처음 나온 순서.
    - 같은 통화는 나중 호출 값으로 덮어쓴다(같은 턴이라 Spring 값은 사실상 같다).
    - unavailableCurrencies는 합친 뒤 카드에 실제로 있는 통화를 뺀다.
    """
    index = next((i for i, b in enumerate(blocks) if b.get("type") == FX_SUMMARY_CARD), None)
    if index is None:
        blocks.append(card)
        return
    current = blocks[index]
    by_code = {c["currency"]: c for c in current["currencies"]}
    for item in card["currencies"]:
        by_code[item["currency"]] = item
    missing = dict.fromkeys([*current["unavailableCurrencies"], *card["unavailableCurrencies"]])
    blocks[index] = {
        "type": FX_SUMMARY_CARD,
        "currencies": list(by_code.values()),
        "unavailableCurrencies": [c for c in missing if c not in by_code],
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
        try:
            card = summary_block(data)
        except (KeyError, TypeError, ValueError):
            # 카드 형식만 어긋남(LLM 결과는 정상): 답변은 그대로 두고 카드만 내지 않는다.
            logger.warning("fx summary card skipped: invalid card values")
            card = None
        return outcome(
            "SUCCEEDED",
            content,
            measurement={"arguments": arguments, "content": content},
            blocks=[card] if card is not None else [],
        )

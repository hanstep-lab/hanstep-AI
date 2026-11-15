"""챗봇 송금 조회 도구: 받는 분 목록, 참고 환율, 송금 견적. 송금은 실행하지 않는다.

계약은 specs/chat/remittance_contract_v1.md(Spring 내부 API 세 개)를 따른다.
- 사용자 id와 요청 id는 요청마다 고정된다. LLM 인자로는 다른 사용자를 지정할 수 없다.
- 금액은 코드(Spring 견적)만 계산한다. LLM은 견적 결과의 숫자를 옮겨 쓴다.
- 견적이 성공하면 확인 카드(REMITTANCE_CONFIRM, agent.done의 blocks)를 낸다. 카드 숫자는 견적
  결과에서만 채우고, 사용자는 카드의 송금 화면 경로(transferPath)에서 확인하고 보낸다.
- 견적은 그 사용자의 저장된 받는 분에게만, KRW(보낼 금액) 또는 받는 분 통화(받을 금액)로만 낸다.
- LLM에는 isMock을 주지 않는다(금융 도구와 같은 원칙).
- LLM에 주는 환율(get_fx_rate, quote_remittance의 appliedRate)에는 방향이 분명한 rateText
  ("1 PHP = 22.32 KRW")를 코드가 붙인다. 확인 카드 형식은 그대로다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, ValidationError
from pydantic.alias_generators import to_camel

from app.chat.tools import UNAVAILABLE, ToolOutcome
from app.integrations.spring_client import SpringApiError, SpringInternalClient

logger = logging.getLogger(__name__)

GET_REMITTANCE_RECIPIENTS = "get_remittance_recipients"
GET_FX_RATE = "get_fx_rate"
QUOTE_REMITTANCE = "quote_remittance"
REMITTANCE_TOOL_NAMES = (GET_REMITTANCE_RECIPIENTS, GET_FX_RATE, QUOTE_REMITTANCE)
SEND_CURRENCY = "KRW"
# 한 번에 견적을 낼 수 있는 금액 상한(오타로 큰 숫자가 들어가는 것을 막는 입력 검증용).
MAX_QUOTE_AMOUNT = 100_000_000
# 테스트·fake 모드·Spring 미설정 시 쓰는 값(데모, 실제 사용자 데이터가 아님).
FIXTURE = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "chat" / "remittance_context.json"
)

TOOL_SCHEMAS: tuple[dict[str, Any], ...] = (
    {
        "name": GET_REMITTANCE_RECIPIENTS,
        "description": (
            "Get the current user's saved remittance recipients: recipientId, English name, "
            "relationship to the user (MOTHER, SELF, ...), country, currency, bank and the last "
            "4 digits of the account. Money can only be sent to these recipients. "
            "Takes no arguments."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": GET_FX_RATE,
        "description": (
            "Get the reference exchange rate between KRW and a currency: `unit` units of the "
            "currency cost `rate` KRW. A reference value, not a quote."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "currency": {
                    "type": "string",
                    "pattern": "^[A-Z]{3}$",
                    "description": "ISO 4217 code, e.g. PHP",
                }
            },
            "required": ["currency"],
            "additionalProperties": False,
        },
    },
    {
        "name": QUOTE_REMITTANCE,
        "description": (
            "Get a quote for sending money to one saved recipient: the KRW amount to send, the "
            "amount the recipient receives and the applied rate. currency KRW means the user "
            "sends `amount` won; the recipient's currency (e.g. PHP) means the recipient "
            "receives `amount`. Does not send money: the user checks the confirmation card and "
            "sends on the transfer screen. "
            "appliedRate.rateText states the rate as '<unit> <currency> = <rate> KRW'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "recipientId": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "recipientId from get_remittance_recipients",
                },
                "amount": {"type": "integer", "minimum": 1, "maximum": MAX_QUOTE_AMOUNT},
                "currency": {
                    "type": "string",
                    "pattern": "^[A-Z]{3}$",
                    "description": "KRW (amount to send) or the recipient's currency (to receive)",
                },
            },
            "required": ["recipientId", "amount", "currency"],
            "additionalProperties": False,
        },
    },
)


class Contract(BaseModel):
    """Spring 계약·채팅 기록 공통: camelCase, 모르는 필드는 무시, 만든 뒤 바꿀 수 없음."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)


CurrencyCode = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
# 환율은 Decimal로 계산하고 JSON에는 숫자로 내보낸다.
Rate = Annotated[Decimal, Field(gt=0), PlainSerializer(float, return_type=float, when_used="json")]


class Recipient(Contract):
    recipient_id: int = Field(ge=1)
    name_en: str = Field(min_length=1, max_length=100)
    relationship: str = Field(min_length=1, max_length=30)
    country_code: str = Field(pattern=r"^[A-Z]{2}$")
    currency: CurrencyCode
    bank_name: str = Field(min_length=1, max_length=100)
    account_last4: str = Field(pattern=r"^\d{4}$")


class RecipientList(Contract):
    recipients: tuple[Recipient, ...]


class AppliedRate(Contract):
    """``unit`` 외화 = ``rate`` KRW."""

    currency: CurrencyCode
    unit: int = Field(ge=1)
    rate: Rate
    source_label: str = Field(min_length=1, max_length=100)
    as_of: date
    is_mock: bool


class Money(Contract):
    amount: int = Field(ge=0)
    currency: CurrencyCode


class Quote(Contract):
    recipient_id: int = Field(ge=1)
    send_amount: Money
    receive_amount: Money
    applied_rate: AppliedRate


TRANSFER_PATH = "/transfer"
# LLM에 카드가 화면에 나간다는 것만 알린다(카드 내용은 견적 결과와 같다).
CARD_NOTE = "A confirmation card with these amounts is shown to the user."


class RemittanceConfirmBlock(Contract):
    """송금 확인 카드(chat_messages.blocks_json). 사용자가 송금 화면에서 확인하고 보낸다."""

    type: Literal["REMITTANCE_CONFIRM"] = "REMITTANCE_CONFIRM"
    recipient: Recipient
    send_amount: Money
    receive_amount: Money
    applied_rate: AppliedRate
    transfer_path: str = Field(description="송금 화면 경로. amount는 보낼 원화 금액")


def confirm_block(recipient: Recipient, quote: Quote) -> dict[str, Any]:
    """견적 결과만으로 확인 카드를 만든다."""
    query = urlencode({"recipientId": recipient.recipient_id, "amount": quote.send_amount.amount})
    block = RemittanceConfirmBlock(
        recipient=recipient,
        send_amount=quote.send_amount,
        receive_amount=quote.receive_amount,
        applied_rate=quote.applied_rate,
        transfer_path=f"{TRANSFER_PATH}?{query}",
    )
    data: dict[str, Any] = block.model_dump(mode="json", by_alias=True)
    return data


class QuoteInput(BaseModel):
    """quote_remittance 인자(LLM이 채움)."""

    model_config = ConfigDict(extra="forbid")

    recipientId: int = Field(ge=1)  # noqa: N815 - LLM 인자 이름 그대로
    amount: int = Field(ge=1, le=MAX_QUOTE_AMOUNT)
    currency: CurrencyCode


class FxRateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency: CurrencyCode


class NoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RemittanceSource(Protocol):
    """받는 분·환율·견적을 주는 곳(Spring 내부 API 또는 fixture)."""

    async def recipients(self, user_id: int, request_id: str | None) -> RecipientList: ...

    async def fx_rate(self, currency: str, request_id: str | None) -> AppliedRate: ...

    async def quote(
        self, user_id: int, recipient_id: int, amount: int, currency: str, request_id: str | None
    ) -> Quote: ...


def quote_from_rate(recipient_id: int, amount: int, currency: str, rate: AppliedRate) -> Quote:
    """fixture 견적: 보낼 KRW → 받을 금액은 내림, 받을 금액 → 보낼 KRW는 올림(정수).

    운영 견적은 Spring이 계산한다. 이 규칙은 fake 모드·테스트에서만 쓴다.
    """
    per_unit = rate.rate / rate.unit
    if currency == SEND_CURRENCY:
        send = amount
        receive = int((Decimal(amount) / per_unit).to_integral_value(rounding=ROUND_FLOOR))
    else:
        receive = amount
        send = int((Decimal(amount) * per_unit).to_integral_value(rounding=ROUND_CEILING))
    return Quote(
        recipient_id=recipient_id,
        send_amount=Money(amount=send, currency=SEND_CURRENCY),
        receive_amount=Money(amount=receive, currency=rate.currency),
        applied_rate=rate,
    )


class FixtureRemittanceSource:
    """fake 모드이거나 Spring 설정이 없을 때: 데모 받는 분 두 명과 PHP 참고 환율."""

    def __init__(self, path: Path = FIXTURE) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        self._recipients = RecipientList.model_validate(data)
        self._rates = {r["currency"]: AppliedRate.model_validate(r) for r in data["fxRates"]}

    async def recipients(self, user_id: int, request_id: str | None) -> RecipientList:
        return self._recipients

    async def fx_rate(self, currency: str, request_id: str | None) -> AppliedRate:
        if currency not in self._rates:
            raise SpringApiError(404, "FX_RATE_NOT_FOUND", "")
        return self._rates[currency]

    async def quote(
        self, user_id: int, recipient_id: int, amount: int, currency: str, request_id: str | None
    ) -> Quote:
        recipient = next(
            (r for r in self._recipients.recipients if r.recipient_id == recipient_id), None
        )
        if recipient is None:
            raise SpringApiError(404, "RECIPIENT_NOT_FOUND", "")
        if currency not in (SEND_CURRENCY, recipient.currency):
            raise SpringApiError(400, "VALIDATION_ERROR", "")
        rate = await self.fx_rate(recipient.currency, request_id)
        return quote_from_rate(recipient_id, amount, currency, rate)


class SpringRemittanceSource:
    """anthropic 모드 + Spring 설정: Spring 내부 API 세 개."""

    def __init__(self, client: SpringInternalClient, timeout: float) -> None:
        self._client = client
        self._timeout = timeout

    async def recipients(self, user_id: int, request_id: str | None) -> RecipientList:
        data = await asyncio.to_thread(
            self._client.remittance_recipients,
            user_id,
            timeout=self._timeout,
            request_id=request_id,
        )
        return RecipientList.model_validate(data)

    async def fx_rate(self, currency: str, request_id: str | None) -> AppliedRate:
        data = await asyncio.to_thread(
            self._client.fx_rate, currency, timeout=self._timeout, request_id=request_id
        )
        return AppliedRate.model_validate(data)

    async def quote(
        self, user_id: int, recipient_id: int, amount: int, currency: str, request_id: str | None
    ) -> Quote:
        data = await asyncio.to_thread(
            self._client.remittance_quote,
            user_id,
            recipient_id=recipient_id,
            amount=amount,
            currency=currency,
            timeout=self._timeout,
            request_id=request_id,
        )
        return Quote.model_validate(data)


def _for_llm(model: Contract) -> dict[str, Any]:
    """LLM에 줄 JSON. isMock은 뺀다."""
    data: dict[str, Any] = model.model_dump(mode="json", by_alias=True)

    def strip(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k != "isMock"}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    result: dict[str, Any] = strip(data)
    return result


def rate_text(rate: AppliedRate) -> str:
    """환율을 방향이 분명한 문장으로: "1 PHP = 22.32 KRW", "100 VND = 5.43 KRW".

    rate는 끝자리 0을 빼고 지수 표기 없이 쓴다(22.30 → 22.3, 1E+3 → 1000).
    """
    value = format(rate.rate.normalize(), "f")
    return f"{rate.unit} {rate.currency} = {value} KRW"


def _rate_for_llm(rate: AppliedRate) -> dict[str, Any]:
    """LLM에 줄 환율: 계약 필드(isMock 제외) + 코드가 만든 rateText. 확인 카드에는 넣지 않는다."""
    return {**_for_llm(rate), "rateText": rate_text(rate)}


# Spring 4xx 중 인자를 고치거나 사용자에게 물어야 하는 것: LLM에 알려 줄 문장.
REJECTIONS = {
    "RECIPIENT_NOT_FOUND": (
        "this recipient is not one of the user's saved recipients; use a recipientId from "
        "get_remittance_recipients"
    ),
    "FX_RATE_NOT_FOUND": "no reference rate for this currency",
    "VALIDATION_ERROR": (
        "invalid amount or currency: the currency must be KRW or the recipient's currency"
    ),
}

OutcomeFn = Callable[..., ToolOutcome]


class RemittanceTools:
    """요청 하나에 묶인 송금 조회 도구 세 개."""

    def __init__(
        self,
        source: RemittanceSource,
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
        return [dict(schema) for schema in TOOL_SCHEMAS]

    async def run(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        started = time.perf_counter()

        def outcome(status: str, content: Any, **extra: Any) -> ToolOutcome:
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            latency = round((time.perf_counter() - started) * 1000)
            return ToolOutcome(name, arguments, status, text, latency, **extra)

        inputs: dict[str, type[BaseModel]] = {
            GET_REMITTANCE_RECIPIENTS: NoInput,
            GET_FX_RATE: FxRateInput,
            QUOTE_REMITTANCE: QuoteInput,
        }
        if name not in inputs:
            return outcome("REJECTED", "unknown tool", error_code="UNKNOWN_TOOL")
        try:
            parsed = inputs[name].model_validate(arguments)
        except ValidationError as exc:
            fields = [".".join(str(p) for p in e["loc"]) + f" ({e['type']})" for e in exc.errors()]
            return outcome(
                "REJECTED",
                f"invalid arguments: {', '.join(fields)}",
                error_code="INVALID_ARGUMENTS",
            )
        if isinstance(parsed, FxRateInput):
            return await self._call(outcome, lambda: self._fx_rate(parsed, outcome))
        if isinstance(parsed, QuoteInput):
            return await self._call(outcome, lambda: self._quote(parsed, outcome))
        return await self._call(outcome, lambda: self._recipients(outcome))

    async def _call(
        self, outcome: OutcomeFn, work: Callable[[], Awaitable[ToolOutcome]]
    ) -> ToolOutcome:
        """Spring 호출 공통 처리: 시간 초과·거절·실패를 상태로 바꾼다(예외를 밖으로 내지 않음)."""
        try:
            return await asyncio.wait_for(work(), self._timeout)
        except (TimeoutError, httpx.TimeoutException):
            return outcome("TIMEOUT", UNAVAILABLE, error_code="SPRING_TIMEOUT")
        except SpringApiError as exc:
            error_code = f"SPRING_HTTP_{exc.status}"
            rejection = REJECTIONS.get(exc.code)
            if rejection is not None and 400 <= exc.status < 500:
                return outcome("REJECTED", rejection, error_code=error_code)
            return outcome("FAILED", UNAVAILABLE, error_code=error_code)
        except ValidationError:
            return outcome("FAILED", UNAVAILABLE, error_code="INVALID_RESPONSE")
        except Exception as exc:  # 연결 실패 등: 송금 정보 없이 답하게 한다
            logger.warning("remittance tool failed: %s", type(exc).__name__)
            return outcome("FAILED", UNAVAILABLE, error_code="SPRING_ERROR")

    async def _recipients(self, outcome: OutcomeFn) -> ToolOutcome:
        recipients = await self._source.recipients(self._user_id, self._request_id)
        return outcome("SUCCEEDED", _for_llm(recipients))

    async def _fx_rate(self, parsed: FxRateInput, outcome: OutcomeFn) -> ToolOutcome:
        rate = await self._source.fx_rate(parsed.currency, self._request_id)
        if rate.currency != parsed.currency:
            return outcome("FAILED", UNAVAILABLE, error_code="INVALID_RESPONSE")
        return outcome("SUCCEEDED", _rate_for_llm(rate))

    async def _quote(self, parsed: QuoteInput, outcome: OutcomeFn) -> ToolOutcome:
        listed = await self._source.recipients(self._user_id, self._request_id)
        recipient = next(
            (r for r in listed.recipients if r.recipient_id == parsed.recipientId), None
        )
        if recipient is None:
            return outcome(
                "REJECTED", REJECTIONS["RECIPIENT_NOT_FOUND"], error_code="UNKNOWN_RECIPIENT"
            )
        if parsed.currency not in (SEND_CURRENCY, recipient.currency):
            return outcome(
                "REJECTED",
                f"currency must be {SEND_CURRENCY} (amount to send) or {recipient.currency} "
                "(amount the recipient receives)",
                error_code="UNSUPPORTED_CURRENCY",
            )
        quote = await self._source.quote(
            self._user_id, recipient.recipient_id, parsed.amount, parsed.currency, self._request_id
        )
        if not _quote_matches(quote, recipient, parsed):
            return outcome("FAILED", UNAVAILABLE, error_code="INVALID_RESPONSE")
        content = {
            "recipient": _for_llm(recipient),
            **_for_llm(quote),
            "appliedRate": _rate_for_llm(quote.applied_rate),
            "note": CARD_NOTE,
        }
        return outcome("SUCCEEDED", content, blocks=[confirm_block(recipient, quote)])


def _quote_matches(quote: Quote, recipient: Recipient, parsed: QuoteInput) -> bool:
    """Spring 견적이 요청한 받는 분·통화·금액과 맞는지(다르면 카드를 내지 않는다)."""
    asked = quote.send_amount if parsed.currency == SEND_CURRENCY else quote.receive_amount
    return (
        quote.recipient_id == recipient.recipient_id
        and quote.send_amount.currency == SEND_CURRENCY
        and quote.receive_amount.currency == recipient.currency
        and quote.applied_rate.currency == recipient.currency
        and asked.amount == parsed.amount
    )

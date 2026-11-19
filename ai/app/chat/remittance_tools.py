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
- Spring 견적(INT-18)은 기본 상품(defaultProduct, 지금은 쏠편한) 하나를 기준으로 쓴다(#105).
  - 수수료·환전 마진·출금 총액·우대율·USD 송금 환율·안내 코드는 Spring 값 그대로 쓰고, AI는
    계산하지 않는다.
  - LLM에는 영어 답변 안내(answerGuide)를 붙인다. 송금 환율은 1 USD = … KRW이고, PHP 환율은
    참고 금액용이다.
  - 카드에는 코드·숫자만 더한다(fees, rateSources). 한국어 출처 문구(sources)는 LLM에만 준다.
  - 기본 상품이 보낼 수 없는 금액이면(available=false) 카드 없이 이유를 돌려준다.
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
            "Get a quote for sending money to one saved recipient. currency KRW means `amount` "
            "won leave the user's account in total, fees included; the recipient's currency "
            "(e.g. PHP) means the recipient receives `amount`. The money is sent in USD: the "
            "result has the USD amount, the remittance rate (1 USD = ... KRW), the fees, the "
            "reference amount in the recipient's currency, notice codes and answerGuide. "
            "Follow answerGuide when you reply. Does not send money: the user checks the "
            "confirmation card and sends on the transfer screen. "
            "Rates are stated as '<unit> <currency> = <rate> KRW' (rateText)."
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


def _plain_number(value: Decimal) -> int | float:
    """정수면 정수(70), 아니면 소수(370.81)로. Spring JSON과 같은 모양으로 낸다."""
    return int(value) if value == value.to_integral_value() else float(value)


# 퍼센트·USD 금액처럼 0일 수 있는 소수(Spring BigDecimal).
Number = Annotated[
    Decimal, PlainSerializer(_plain_number, return_type=int | float, when_used="json")
]


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


class QuoteRates(Contract):
    """견적에 쓴 환율(1 외화 당 원). PHP 참고 환율이 없으면 php* 값이 모두 null이다."""

    usd_base_rate: Rate
    usd_rate_date: date
    usd_source: str = Field(min_length=1)
    php_ref_rate: Rate | None = None
    php_rate_date: date | None = None
    php_source: str | None = None
    usd_php_ref_rate: Rate | None = None


class QuoteSource(Contract):
    """값 하나의 출처. title·approvalNo는 한국어라 LLM에만 주고 카드에는 넣지 않는다."""

    item: str
    title: str
    approval_no: str | None = None
    checked_on: date


class ProductQuote(Contract):
    """상품 하나의 견적(Spring RemittanceProductQuote). available=false면 금액 칸은 null."""

    product: str = Field(min_length=1)
    available: bool
    unavailable_reason: str | None = None
    send_amount_usd: Number | None = None
    spread_pct: Number
    preferential_pct: Number
    # to_camel은 1y를 1Y로 바꾸므로 별칭을 직접 둔다.
    remittance_count_1y: int | None = Field(default=None, alias="remittanceCount1y")
    usd_applied_rate: Rate
    krw_principal: int | None = None
    fx_margin_krw: int | None = Field(
        default=None, description="환전 마진(원). krwPrincipal에 이미 들어 있어 더하지 않는다"
    )
    send_fee_krw: int | None = None
    telegraphic_fee_krw: int | None = None
    fee_usd: Number | None = None
    total_withdraw_krw: int | None = None
    unused_krw: int | None = None
    recipient_amount_usd: Number | None = None
    recipient_amount_php_ref: Number | None = None
    delivery_type: str
    notices: tuple[str, ...]
    sources: tuple[QuoteSource, ...]


class Quote(Contract):
    """Spring 견적(RemittanceQuoteResponse) 중 AI가 쓰는 값.

    receiveAmount·appliedRate는 PHP 참고값이다. PHP 참고 환율이 없거나 기본 상품 견적이 없으면
    null이다. defaultProduct·rates·quotes가 없는 견적(fixture)은 #105 전과 같이 다룬다.
    """

    recipient_id: int = Field(ge=1)
    send_amount: Money
    receive_amount: Money | None
    applied_rate: AppliedRate | None
    default_product: str | None = None
    rates: QuoteRates | None = None
    quotes: tuple[ProductQuote, ...] = ()

    def product_quote(self) -> ProductQuote | None:
        """기본 상품의 견적. 상품 견적이 없는 견적(fixture)이면 None."""
        return next((q for q in self.quotes if q.product == self.default_product), None)


TRANSFER_PATH = "/transfer"
# LLM에 카드가 화면에 나간다는 것만 알린다(카드 내용은 견적 결과와 같다).
CARD_NOTE = "A confirmation card with these amounts is shown to the user."


class RemittanceFees(Contract):
    """카드의 수수료·환율 값(코드·숫자만). 화면 문구는 앱이 화면 언어로 만든다."""

    product: str
    send_fee_krw: int | None
    telegraphic_fee_krw: int | None
    fee_usd: Number | None
    fx_margin_krw: int | None
    total_withdraw_krw: int | None
    preferential_pct: Number
    usd_applied_rate: Rate = Field(description="송금 환율(1 USD 당 원)")
    notices: tuple[str, ...] = Field(description="안내 코드")


class RateSources(Contract):
    """환율 출처 코드. 앱이 코드로 문구를 고른다."""

    usd: str = Field(description="USD 매매기준율 출처(예: KOREAEXIM)")
    php: str | None = Field(description="PHP 참고 환율 출처(예: frankfurter-ecb). 없으면 null")


class RemittanceConfirmBlock(Contract):
    """송금 확인 카드(chat_messages.blocks_json). 사용자가 송금 화면에서 확인하고 보낸다."""

    type: Literal["REMITTANCE_CONFIRM"] = "REMITTANCE_CONFIRM"
    recipient: Recipient
    send_amount: Money
    receive_amount: Money | None = Field(description="PHP 참고 금액. PHP 참고 환율이 없으면 null")
    applied_rate: AppliedRate | None = Field(
        description="PHP 참고 환율(송금 환율 아님). PHP 참고 환율이 없으면 null"
    )
    transfer_path: str = Field(description="송금 화면 경로. amount는 보낼 원화 금액")
    fees: RemittanceFees | None = Field(
        default=None, description="기본 상품 수수료·환율(#105). 상품 견적이 없는 fixture면 null"
    )
    rate_sources: RateSources | None = Field(
        default=None, description="환율 출처 코드(#105). 상품 견적이 없는 fixture면 null"
    )


def confirm_block(
    recipient: Recipient, quote: Quote, product: ProductQuote | None = None
) -> dict[str, Any]:
    """견적 결과만으로 확인 카드를 만든다."""
    query = urlencode({"recipientId": recipient.recipient_id, "amount": quote.send_amount.amount})
    fees = None
    rate_sources = None
    if product is not None and quote.rates is not None:
        fees = RemittanceFees(
            product=product.product,
            send_fee_krw=product.send_fee_krw,
            telegraphic_fee_krw=product.telegraphic_fee_krw,
            fee_usd=product.fee_usd,
            fx_margin_krw=product.fx_margin_krw,
            total_withdraw_krw=product.total_withdraw_krw,
            preferential_pct=product.preferential_pct,
            usd_applied_rate=product.usd_applied_rate,
            notices=product.notices,
        )
        rate_sources = RateSources(usd=quote.rates.usd_source, php=quote.rates.php_source)
    block = RemittanceConfirmBlock(
        recipient=recipient,
        send_amount=quote.send_amount,
        receive_amount=quote.receive_amount,
        applied_rate=quote.applied_rate,
        transfer_path=f"{TRANSFER_PATH}?{query}",
        fees=fees,
        rate_sources=rate_sources,
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


# --- 기본 상품 견적 → LLM(#105) ----------------------------------------------------------
# 문구는 LLM 입력에만 쓴다(답변은 영어). 카드에는 코드만 싣고 앱이 화면 언어로 문구를 고른다.

NOTICE_TEXTS = {
    "INTERMEDIARY_FEE_MAY_APPLY": (
        "Intermediary or receiving bank fees may be deducted; the amount is not known in advance."
    ),
    "LOCAL_PAYOUT_RATE_BY_AGENT": "The payout agent sets the local payout rate.",
    "LOCAL_PAYOUT_RATE_DIFFERS": (
        "The recipient's bank converts the USD at its own rate, so the local amount is only a "
        "reference."
    ),
    "PHP_REF_RATE_UNAVAILABLE": "There is no reference rate for the local currency.",
    "FEE_TABLE_UNAVAILABLE": "The fee table for this product is not available.",
    "AMOUNT_CAPPED_AT_FEE_BOUNDARY": (
        "The amount sent stops at the fee boundary to keep the lower fee; unusedKrw stays in the "
        "user's account."
    ),
    "FOREIGN_EXCHANGE_BANK_DESIGNATION_REQUIRED": (
        "Overseas remittances need a designated foreign exchange bank."
    ),
}
UNAVAILABLE_TEXTS = {
    "AMOUNT_TOO_SMALL": "The amount is too small: nothing is left to send after the fees.",
    "LIMIT_EXCEEDED": "The amount is over this product's per-transfer limit.",
    "FEE_TABLE_UNAVAILABLE": "This product's fee table is not available now.",
}
# 출처(item) → 답변에 쓸 짧은 영어 이름. 한국어 문서 제목·심사필 번호는 답변에 옮기지 않는다.
SOURCE_NAMES = {
    "usdBaseRate": "the Export-Import Bank of Korea base rate",
    "spreadPct": "Shinhan Bank's published exchange rate table",
    "preferentialPct": "Shinhan's published fee table",
    "sendFeeKrw": "Shinhan's published fee table",
    "telegraphicFeeKrw": "Shinhan's published fee table",
    "feeUsd": "Shinhan's published product terms",
    "perTransferLimitUsd": "Shinhan's published product terms",
    "remittanceCount1y": "the user's remittance history",
    "phpRefRate": "the ECB reference rate",
}
INTERMEDIARY_FEE = "INTERMEDIARY_FEE_MAY_APPLY"


def _number_text(value: Decimal) -> str:
    """금액·환율을 답변에 쓰는 모양으로: 1,334.91 / 5,000 / 3.5."""
    return f"{value.normalize():,f}"


def _from_sample(product: ProductQuote) -> bool:
    """1년 송금 횟수가 Mock 거래내역에서 왔는지(출처 문구에 Mock이 있다)."""
    return any(s.item == "remittanceCount1y" and "mock" in s.title.lower() for s in product.sources)


def _answer_guide(quote: Quote, product: ProductQuote) -> list[str]:
    """견적 하나에 맞춘 영어 답변 안내. 숫자는 모두 이 견적 값이다."""
    usd_rate = f"1 USD = {_number_text(product.usd_applied_rate)} KRW"
    guide = [
        f"KRW {quote.send_amount.amount:,} is the total that leaves the user's account, fees "
        "included. Say that the fees come out of it.",
    ]
    fees = []
    if product.send_fee_krw:
        fees.append(f"a KRW {product.send_fee_krw:,} sending fee")
    if product.telegraphic_fee_krw:
        fees.append(f"a KRW {product.telegraphic_fee_krw:,} telegraphic fee")
    if product.fee_usd:
        fees.append(f"a USD {_number_text(product.fee_usd)} fee")
    if fees:
        guide.append(f"Name each fee with its amount: {', '.join(fees)}.")
    if product.fx_margin_krw:
        guide.append(
            f"Say the exchange margin is about KRW {product.fx_margin_krw:,}. It is built into "
            "the remittance rate, not an extra charge."
        )
    guide.append(
        f"The money is sent in USD at the remittance rate {usd_rate}. This is the only "
        "remittance rate."
    )
    if quote.receive_amount is not None and quote.applied_rate is not None:
        received = quote.receive_amount
        guide.append(
            f"Say the recipient gets about {received.amount:,} {received.currency} (reference), "
            f"because the recipient's bank converts the USD at its own rate. "
            f"{rate_text(quote.applied_rate)} is only a reference rate for that amount: never "
            "call it the remittance rate or the applied rate."
        )
    elif product.recipient_amount_usd is not None:
        guide.append(
            "There is no reference amount in the recipient's currency. Say the recipient gets "
            f"USD {_number_text(product.recipient_amount_usd)} before intermediary bank fees."
        )
    if INTERMEDIARY_FEE in product.notices:
        guide.append("Say that intermediary bank fees may also apply, without an amount.")
    if product.unused_krw:
        guide.append(f"Say that KRW {product.unused_krw:,} is not sent and stays in the account.")
    if product.preferential_pct:
        basis = (
            "the user's sample remittance history (an estimate)"
            if _from_sample(product)
            else "the user's remittance history"
        )
        guide.append(
            f"If you mention the {_number_text(product.preferential_pct)}% preferential rate, "
            f"say it is based on {basis}. Do not mention any other discount."
        )
    guide += [
        "If you mention a source, name it briefly in English with sourceName (for example, "
        "based on Shinhan's published fee table). Never copy source titles or approval numbers.",
        "Do not call any rate live or real-time. These amounts are estimates from published "
        "rates and fees.",
    ]
    return guide


def _quote_for_llm(recipient: Recipient, quote: Quote, product: ProductQuote) -> dict[str, Any]:
    """기본 상품 견적 → LLM 입력. 값은 Spring 그대로이고, rateText와 안내만 코드가 붙인다."""
    assert quote.rates is not None
    data = _for_llm(product)
    reference = None
    if quote.receive_amount is not None and quote.applied_rate is not None:
        reference = {
            "amount": quote.receive_amount.amount,
            "currency": quote.receive_amount.currency,
            "rate": {**_rate_for_llm(quote.applied_rate), "source": quote.rates.php_source},
            "note": "Reference only, not the amount the recipient is sure to get.",
        }
    return {
        "recipient": _for_llm(recipient),
        "sendAmount": _for_llm(quote.send_amount),
        "product": product.product,
        "deliveryType": product.delivery_type,
        "sendCurrency": "USD",
        "sendAmountUsd": data["sendAmountUsd"],
        "remittanceRate": {
            "rate": data["usdAppliedRate"],
            "rateText": f"1 USD = {_number_text(product.usd_applied_rate)} KRW",
        },
        "usdBaseRate": {
            "rate": _for_llm(quote.rates)["usdBaseRate"],
            "source": quote.rates.usd_source,
            "asOf": quote.rates.usd_rate_date.isoformat(),
        },
        "fees": {
            key: data[key]
            for key in (
                "sendFeeKrw",
                "telegraphicFeeKrw",
                "feeUsd",
                "fxMarginKrw",
                "totalWithdrawKrw",
                "unusedKrw",
            )
        },
        "preferentialPct": data["preferentialPct"],
        "remittanceCount1y": data["remittanceCount1y"],
        "recipientAmountUsd": data["recipientAmountUsd"],
        "referenceAmount": reference,
        "notices": [
            {"code": code, "text": NOTICE_TEXTS.get(code, code)} for code in product.notices
        ],
        "sources": [
            {**s, "sourceName": SOURCE_NAMES.get(s["item"], "Shinhan's published information")}
            for s in data["sources"]
        ],
        "answerGuide": _answer_guide(quote, product),
        "note": CARD_NOTE,
    }


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
        product = quote.product_quote()
        if product is None and quote.default_product is not None:
            return outcome("FAILED", UNAVAILABLE, error_code="INVALID_RESPONSE")
        if product is None:
            # 상품 견적이 없는 견적(fixture): #105 전과 같은 모양.
            if quote.receive_amount is None or quote.applied_rate is None:
                return outcome("FAILED", UNAVAILABLE, error_code="INVALID_RESPONSE")
            content = {
                "recipient": _for_llm(recipient),
                "recipientId": quote.recipient_id,
                "sendAmount": _for_llm(quote.send_amount),
                "receiveAmount": _for_llm(quote.receive_amount),
                "appliedRate": _rate_for_llm(quote.applied_rate),
                "note": CARD_NOTE,
            }
            return outcome("SUCCEEDED", content, blocks=[confirm_block(recipient, quote)])
        if not product.available:
            reason = product.unavailable_reason or "UNAVAILABLE"
            content = {
                "available": False,
                "product": product.product,
                "sendAmount": _for_llm(quote.send_amount),
                "unavailableReason": reason,
                "reason": UNAVAILABLE_TEXTS.get(reason, "This amount cannot be sent."),
            }
            return outcome(
                "REJECTED",
                content,
                error_code="QUOTE_UNAVAILABLE",
                measurement={"arguments": parsed.model_dump(), "content": content},
            )
        if quote.rates is None:
            return outcome("FAILED", UNAVAILABLE, error_code="INVALID_RESPONSE")
        content = _quote_for_llm(recipient, quote, product)
        return outcome(
            "SUCCEEDED",
            content,
            blocks=[confirm_block(recipient, quote, product)],
            # 측정(evaluate_chat)의 숫자 검사 근거: LLM에 준 견적 값.
            measurement={"arguments": parsed.model_dump(), "content": content},
        )


def _quote_matches(quote: Quote, recipient: Recipient, parsed: QuoteInput) -> bool:
    """Spring 견적이 요청한 받는 분·통화·금액과 맞는지(다르면 카드를 내지 않는다).

    receiveAmount·appliedRate는 PHP 참고 환율이 없거나 기본 상품이 보낼 수 없을 때 null이다.
    있을 때만 통화를 본다.
    """
    asked = quote.send_amount if parsed.currency == SEND_CURRENCY else quote.receive_amount
    return (
        quote.recipient_id == recipient.recipient_id
        and quote.send_amount.currency == SEND_CURRENCY
        and (quote.receive_amount is None or quote.receive_amount.currency == recipient.currency)
        and (quote.applied_rate is None or quote.applied_rate.currency == recipient.currency)
        and asked is not None
        and asked.amount == parsed.amount
    )

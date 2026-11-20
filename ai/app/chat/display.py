"""챗봇 답변의 숫자·통화 표시(공통, #116). 규칙은 specs/chat/answer_display_v1.md에 정리했다.

LLM은 숫자를 바꾸지 않는다. 도구가 LLM에 주는 값에 원래 숫자와 함께 이 모듈이 만든 표시용
문자열을 넣고, 프롬프트(v4.6 표시 규칙)가 그 문자열을 그대로 옮기게 한다.

- 표시 언어: 채팅 요청 language가 ko면 한국어 규칙, 그 밖(en·vi·zh)은 영어 규칙이다.
  답변 언어는 프롬프트의 {language}가 정한다(vi·zh 답변도 숫자·통화 표기만 영어 규칙).
- 원화 금액: 정수, 원 미만 버림(0 쪽으로), 천 단위 쉼표. ko "700,000원", en "KRW 700,000".
- 환율·평균: 통화별 자릿수로 반올림(ROUND_HALF_UP)하고 천 단위 쉼표를 붙인다.
  자릿수는 앱 환율 카드(hanstep-frontend src/api/fx.ts AVG_DECIMALS)와 같다: USD 2, PHP 3, 그 밖 2.
  1335.4375(USD) → "1,335.44", 20.984(PHP) → "20.984".
- 통화명: ko는 처음 한 번 "미국 달러(USD)", 그다음부터 "달러". en은 통화 코드.
- float는 str()을 거쳐 Decimal로 바꾼다. 2진 오차 없이 자르고 반올림한다(1.005 → "1.01").
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Literal

DisplayLanguage = Literal["ko", "en"]
Number = int | float | Decimal

KRW = "KRW"
KST = timezone(timedelta(hours=9))
# 통화별 환율 소수 자릿수(앱 환율 카드와 같음). 표에 없는 통화는 DEFAULT_RATE_DECIMALS.
RATE_DECIMALS = {"USD": 2, "PHP": 3}
DEFAULT_RATE_DECIMALS = 2
# 한국어 통화명: (처음 쓸 이름, 그다음부터 쓸 이름). 표에 없는 통화는 코드만 쓴다.
KO_CURRENCY_NAMES = {
    "USD": ("미국 달러", "달러"),
    "PHP": ("필리핀 페소", "페소"),
    "VND": ("베트남 동", "동"),
}
EN_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def display_language(language: str) -> DisplayLanguage:
    """채팅 요청 language → 표시 규칙 언어. ko만 한국어, 그 밖(en·vi·zh)은 영어."""
    return "ko" if language == "ko" else "en"


def _decimal(value: Number) -> Decimal:
    """표시할 숫자 → Decimal. 숫자가 아니면 TypeError, NaN·무한대면 ValueError."""
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise TypeError(f"not a number to display: {type(value).__name__}")
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    if not number.is_finite():
        raise ValueError("not a finite number")
    return number


def _grouped(value: Decimal, places: int) -> str:
    """천 단위 쉼표. 반올림·버림 뒤 0이면 부호를 떼어 "-0.00"이 나오지 않게 한다."""
    return format(abs(value) if value.is_zero() else value, f",.{places}f")


def krw_number_text(amount: Number) -> str:
    """원화 금액 숫자: 원 미만 버림(0 쪽으로), 천 단위 쉼표. 675317.9 → "675,317"."""
    return _grouped(_decimal(amount).quantize(Decimal(1), rounding=ROUND_DOWN), 0)


def krw_text(amount: Number, language: DisplayLanguage) -> str:
    """원화 금액. ko "700,000원", en "KRW 700,000"(음수는 "-KRW 1,234")."""
    number = krw_number_text(amount)
    if language == "ko":
        return f"{number}원"
    return f"-{KRW} {number[1:]}" if number.startswith("-") else f"{KRW} {number}"


def rate_decimals(currency: str) -> int:
    return RATE_DECIMALS.get(currency, DEFAULT_RATE_DECIMALS)


def _rounded_rate(value: Number, currency: str) -> Decimal:
    step = Decimal(1).scaleb(-rate_decimals(currency))
    return _decimal(value).quantize(step, rounding=ROUND_HALF_UP)


def rate_number_text(rate: Number, currency: str) -> str:
    """환율·평균 숫자(통화 표시 없음). 통화별 자릿수로 반올림. 1335.4375(USD) → "1,335.44"."""
    return _grouped(_rounded_rate(rate, currency), rate_decimals(currency))


def signed_rate_number_text(change: Number, currency: str) -> str:
    """부호 있는 변동값(전 영업일 대비, 평균과의 차이). -1.08 → "-1.08", 0.112(PHP) → "+0.112".

    반올림해서 0이면 부호 없이 "0.00"이다.
    """
    rounded = _rounded_rate(change, currency)
    text = _grouped(rounded, rate_decimals(currency))
    return f"+{text}" if rounded > 0 else text


def currency_name(currency: str, language: DisplayLanguage, *, first: bool = True) -> str:
    """통화명. ko: 처음 "미국 달러(USD)", 그다음 "달러", 원화는 "원". en: 통화 코드."""
    if language == "en":
        return currency
    if currency == KRW:
        return "원"
    names = KO_CURRENCY_NAMES.get(currency)
    if names is None:
        return currency
    return f"{names[0]}({currency})" if first else names[1]


def rate_text(rate: Number, currency: str, unit: int, language: DisplayLanguage) -> str:
    """환율 한 줄(KRW per unit). ko "1,330.80원"·"100동당 5.43원", en "1 USD = 1,330.80 KRW"."""
    number = rate_number_text(rate, currency)
    if language == "en":
        return f"{unit:,} {currency} = {number} {KRW}"
    if unit == 1:
        return f"{number}원"
    name = currency_name(currency, language, first=False)
    per = f"{unit:,}{name}당" if name != currency else f"{unit:,} {currency}당"
    return f"{per} {number}원"


def as_of_text(as_of: date, checked_at: datetime | None, language: DisplayLanguage) -> str:
    """기준 시각. 확인 시각(KST)이 기준일과 같은 날이면 시각까지, 아니면 날짜만.

    ko "11월 20일 10:42"·"11월 17일", en "Nov 20, 10:42"·"Nov 17".
    """
    if language == "ko":
        day = f"{as_of.month}월 {as_of.day}일"
    else:
        day = f"{EN_MONTHS[as_of.month - 1]} {as_of.day}"
    if checked_at is None:
        return day
    local = checked_at.astimezone(KST) if checked_at.tzinfo else checked_at
    if local.date() != as_of:
        return day
    clock = f"{local.hour:02d}:{local.minute:02d}"
    return f"{day} {clock}" if language == "ko" else f"{day}, {clock}"

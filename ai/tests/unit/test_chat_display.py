"""챗봇 답변 표시 모듈(app.chat.display, #116). 규칙은 specs/chat/answer_display_v1.md."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.api.routes.chat as chat_routes
from app.chat.display import (
    as_of_text,
    currency_name,
    display_language,
    krw_number_text,
    krw_text,
    rate_number_text,
    rate_text,
    signed_rate_number_text,
)
from app.chat.number_check import check_answer
from app.core.config import Settings
from app.integrations.policy_gateway import UnconfiguredPolicyGateway
from app.main import AppState, create_app
from app.rag.embeddings import BgeM3Embedder
from tests.unit.test_chat_stream import STREAM, TOKEN, parse_sse

pytestmark = pytest.mark.unit

KST = timezone(timedelta(hours=9))


@pytest.mark.parametrize(
    ("language", "expected"),
    [("ko", "ko"), ("en", "en"), ("vi", "en"), ("zh", "en"), ("", "en")],
)
def test_only_ko_gets_korean_rules(language: str, expected: str) -> None:
    # vi·zh는 고려·테스트하지 않는 언어라 영어 규칙으로 간다(답변 언어는 프롬프트가 정한다).
    assert display_language(language) == expected


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (0, "0"),
        (999, "999"),
        (1000, "1,000"),
        (675317, "675,317"),
        (1234567890, "1,234,567,890"),
        # 원 미만은 버린다(반올림하지 않음).
        (675317.9, "675,317"),
        (0.995, "0"),
        (999.999, "999"),
        (Decimal("1000.5"), "1,000"),
        # 음수도 0 쪽으로 버린다. 버린 뒤 0이면 부호가 없다.
        (-1234.9, "-1,234"),
        (-0.5, "0"),
    ],
)
def test_krw_amount_is_floored_with_commas(amount: float, expected: str) -> None:
    assert krw_number_text(amount) == expected


def test_krw_text_by_language() -> None:
    assert krw_text(700000, "ko") == "700,000원"
    assert krw_text(700000.99, "en") == "KRW 700,000"
    assert krw_text(-1234, "ko") == "-1,234원"
    assert krw_text(-1234, "en") == "-KRW 1,234"


@pytest.mark.parametrize(
    ("rate", "currency", "expected"),
    [
        # 공식 고시(소수 둘째 자리)는 그대로다.
        (1330.8, "USD", "1,330.80"),
        (1406.51, "USD", "1,406.51"),
        # 평균은 반올림한다(1335.4375 → 1,335.44).
        (1335.4375, "USD", "1,335.44"),
        (1407.7735, "USD", "1,407.77"),
        # 반올림 경계: float 2진 오차 없이 ROUND_HALF_UP.
        (0.995, "USD", "1.00"),
        (0.005, "USD", "0.01"),
        (0.004, "USD", "0.00"),
        (1.005, "USD", "1.01"),
        (1335.435, "USD", "1,335.44"),
        (999.995, "USD", "1,000.00"),
        # PHP는 카드와 같은 소수 셋째 자리.
        (20.984, "PHP", "20.984"),
        (21.0393, "PHP", "21.039"),
        (21.0395, "PHP", "21.040"),
        (23.8157, "PHP", "23.816"),
        # 표에 없는 통화는 둘째 자리.
        (5.4321, "VND", "5.43"),
        (Decimal("1330.8"), "USD", "1,330.80"),
    ],
)
def test_rate_is_rounded_to_card_digits(rate: float, currency: str, expected: str) -> None:
    assert rate_number_text(rate, currency) == expected


@pytest.mark.parametrize(
    ("change", "currency", "expected"),
    [
        (-1.08, "USD", "-1.08"),
        (-23.1466, "USD", "-23.15"),
        (-1.2635, "USD", "-1.26"),
        (1.08, "USD", "+1.08"),
        (0.112, "PHP", "+0.112"),
        (-0.0553, "PHP", "-0.055"),
        (-2.8317, "PHP", "-2.832"),
        (0, "USD", "0.00"),
        # 반올림해서 0이면 부호가 없다("-0.00"·"+0.00"이 아님).
        (-0.004, "USD", "0.00"),
        (0.004, "USD", "0.00"),
        (-0.005, "USD", "-0.01"),
        (-1234.5, "USD", "-1,234.50"),
    ],
)
def test_signed_change_keeps_the_sign(change: float, currency: str, expected: str) -> None:
    assert signed_rate_number_text(change, currency) == expected


@pytest.mark.parametrize("value", [True, None, "1406.51", float("nan"), float("inf")])
def test_non_numbers_are_rejected(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        rate_number_text(value, "USD")  # type: ignore[arg-type]


def test_currency_names_by_language() -> None:
    assert currency_name("USD", "ko") == "미국 달러(USD)"
    assert currency_name("USD", "ko", first=False) == "달러"
    assert currency_name("PHP", "ko") == "필리핀 페소(PHP)"
    assert currency_name("PHP", "ko", first=False) == "페소"
    assert currency_name("VND", "ko") == "베트남 동(VND)"
    assert currency_name("VND", "ko", first=False) == "동"
    # 원화는 "원"만, 표에 없는 통화는 코드만.
    assert currency_name("KRW", "ko") == "원"
    assert currency_name("THB", "ko") == "THB"
    assert currency_name("THB", "ko", first=False) == "THB"
    for code in ("USD", "PHP", "KRW"):
        assert currency_name(code, "en") == code
        assert currency_name(code, "en", first=False) == code


def test_rate_text_by_language() -> None:
    assert rate_text(1330.8, "USD", 1, "ko") == "1,330.80원"
    assert rate_text(1330.8, "USD", 1, "en") == "1 USD = 1,330.80 KRW"
    assert rate_text(20.984, "PHP", 1, "en") == "1 PHP = 20.984 KRW"
    assert rate_text(5.4321, "VND", 100, "ko") == "100동당 5.43원"
    assert rate_text(5.4321, "VND", 100, "en") == "100 VND = 5.43 KRW"
    assert rate_text(1.23, "THB", 100, "ko") == "100 THB당 1.23원"


def test_as_of_text_has_the_time_only_on_the_as_of_day() -> None:
    same_day = datetime(2026, 11, 20, 10, 42, 7, tzinfo=KST)
    assert as_of_text(date(2026, 11, 20), same_day, "ko") == "11월 20일 10:42"
    assert as_of_text(date(2026, 11, 20), same_day, "en") == "Nov 20, 10:42"
    # 확인 시각이 다음 날이면(MARKET_REF: 11/17 값을 11/18에 확인) 날짜만 쓴다.
    next_day = datetime(2026, 11, 18, 14, 13, 52, tzinfo=KST)
    assert as_of_text(date(2026, 11, 17), next_day, "ko") == "11월 17일"
    assert as_of_text(date(2026, 11, 17), next_day, "en") == "Nov 17"
    # UTC로 와도 KST 날짜로 비교한다(01:42Z = 10:42 KST).
    utc = datetime(2026, 11, 20, 1, 42, tzinfo=UTC)
    assert as_of_text(date(2026, 11, 20), utc, "ko") == "11월 20일 10:42"
    assert as_of_text(date(2026, 11, 6), None, "en") == "Nov 6"


def test_number_check_reads_display_texts_as_the_same_values() -> None:
    # 표시 문자열(쉼표·소수·원·KRW)은 원래 숫자와 같은 값으로 읽힌다. 반올림한 평균은 표시 문자열이
    # 도구 결과에 있을 때만 근거가 있다(검사 기준은 바꾸지 않는다).
    raw = {"unit": 1, "months": 1, "rate": 1330.8, "average": 1335.4375}
    shown = {**raw, "rateText": rate_number_text(1330.8, "USD"), "averageText": "1,335.44"}
    answer = "**오늘 미국 달러(USD) 환율은 1,330.80원이에요.**\n- 1개월 평균 1,335.44원"
    assert check_answer(answer, [], [], fx_results=[shown]).unsupported == []
    assert check_answer(answer, [], [], fx_results=[raw]).unsupported == ["1,335.44원"]
    english = "1 USD = 1,330.80 KRW. 1-month average: 1,335.44 KRW"
    assert check_answer(english, [], [], fx_results=[shown]).unsupported == []


def _fx_tool_kwargs(version: str, language: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: list[dict[str, Any]] = []
    real = chat_routes.FxTools

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(chat_routes, "FxTools", spy)
    values: dict[str, Any] = {
        "ai_internal_api_token": TOKEN,
        "chat_fake_delta_delay_ms": 0,
        "chat_llm_mode": "fake",
        "chat_prompt_version": version,
    }
    settings = Settings(_env_file=None, **values)  # type: ignore[call-arg]
    app = create_app()
    app.state.rag = AppState(
        settings=settings, gateway=UnconfiguredPolicyGateway(), embedder=BgeM3Embedder()
    )
    body = {"runId": "run_" + "d" * 32, "userId": 1, "sessionId": 1, "language": language,
            "history": [], "message": "hi"}  # fmt: skip
    response = TestClient(app).post(STREAM, json=body, headers={"X-Internal-Token": TOKEN})
    assert response.status_code == 200 and parse_sse(response.text)
    [kwargs] = seen
    return kwargs


@pytest.mark.parametrize(("version", "display"), [("v4.4", False), ("v4.4d", True)])
@pytest.mark.parametrize("language", ["ko", "en", "vi"])
def test_route_passes_the_request_language_to_the_fx_tool(
    version: str, display: bool, language: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #111과 같은 방식: 요청마다 도구를 만들 때 language=body.language를 넘긴다(LLM 인자가 아님).
    # 표시값은 표시 규칙이 있는 버전(v4.4d)에서만 켠다.
    kwargs = _fx_tool_kwargs(version, language, monkeypatch)
    assert (kwargs["language"], kwargs["display_text"]) == (language, display)
    assert chat_routes.DISPLAY_PROMPT_VERSIONS == frozenset({"v4.4d"})

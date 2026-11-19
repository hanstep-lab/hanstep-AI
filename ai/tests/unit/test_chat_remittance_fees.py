"""송금 견적의 수수료·환전 마진·안내 코드(#105). 운영 실제 응답(Reyes, 2026-11-19)으로 돈다.

fixture: 운영 GET /api/v1/transfers/quote 응답 그대로(amount 500000·4700, KRW, recipientId 1).
AI가 부르는 내부 API(INT-18)는 같은 DTO다(hanstep-backend TransferApiTest가 두 JSON이 같음을 확인).
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.chat.number_check import check_answer
from app.chat.remittance_tools import (
    QUOTE_REMITTANCE,
    Quote,
    RemittanceTools,
    SpringRemittanceSource,
)
from app.chat.tools import ToolOutcome
from app.integrations.spring_client import SpringInternalClient

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "chat"
TOKEN = "s" * 40
HANGUL = re.compile(r"[가-힣]")
LORNA = {
    "recipientId": 1,
    "nameEn": "LORNA REYES",
    "relationship": "MOTHER",
    "countryCode": "PH",
    "currency": "PHP",
    "bankName": "BDO",
    "accountLast4": "5566",
}
ARGS = {"recipientId": 1, "amount": 500_000, "currency": "KRW"}


def _fixture(amount: int) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (FIXTURES / f"remittance_quote_reyes_{amount}.json").read_text(encoding="utf-8")
    )
    return data


def _easy(quote: dict[str, Any]) -> dict[str, Any]:
    return next(q for q in quote["quotes"] if q["product"] == quote["defaultProduct"])


def _run_quote(quote: dict[str, Any], arguments: dict[str, Any] = ARGS) -> ToolOutcome:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/remittance-recipients"):
            return httpx.Response(200, json={"recipients": [LORNA]})
        return httpx.Response(200, json=quote)

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://spring")
    client = SpringInternalClient(base_url="http://spring", api_key=TOKEN, client=http)
    tools = RemittanceTools(
        SpringRemittanceSource(client, 5), user_id=7, request_id="req_f", timeout=5
    )
    return asyncio.run(tools.run(QUOTE_REMITTANCE, arguments))


def _without_php(quote: dict[str, Any]) -> dict[str, Any]:
    """PHP 참고 환율이 없는 응답(국적이 PHL이 아니거나 ECB 값이 없을 때, Spring 계산 규칙대로)."""
    quote = copy.deepcopy(quote)
    quote.update(receiveAmount=None, appliedRate=None, referenceNotice="PHP_REF_RATE_UNAVAILABLE")
    quote["rates"].update(phpRefRate=None, phpRateDate=None, phpSource=None, usdPhpRefRate=None)
    for q in quote["quotes"]:
        q["recipientAmountPhpRef"] = None
        q["notices"] = [
            "PHP_REF_RATE_UNAVAILABLE" if n == "LOCAL_PAYOUT_RATE_DIFFERS" else n
            for n in q["notices"]
        ]
        q["sources"] = [s for s in q["sources"] if s["item"] != "phpRefRate"]
    return quote


# --- 계약 -----------------------------------------------------------------------------


def test_contract_parses_the_production_quote() -> None:
    quote = Quote.model_validate(_fixture(500_000))
    product = quote.product_quote()
    assert product is not None
    assert (product.product, product.available, product.delivery_type) == (
        "SOL_EASY",
        True,
        "SWIFT_ACCOUNT",
    )
    assert (product.send_fee_krw, product.telegraphic_fee_krw, product.fee_usd) == (0, 5000, None)
    assert (product.fx_margin_krw, product.total_withdraw_krw) == (1413, 499_998)
    assert (str(product.usd_applied_rate), str(product.send_amount_usd)) == ("1334.91", "370.81")
    assert (int(product.preferential_pct), product.remittance_count_1y) == (70, 23)
    assert product.notices == (
        "INTERMEDIARY_FEE_MAY_APPLY",
        "LOCAL_PAYOUT_RATE_DIFFERS",
        "FOREIGN_EXCHANGE_BANK_DESIGNATION_REQUIRED",
    )
    assert quote.rates is not None
    assert (quote.rates.usd_source, quote.rates.php_source) == ("KOREAEXIM", "frankfurter-ecb")


# --- 확인 카드 --------------------------------------------------------------------------


def test_card_fees_and_rate_sources_are_the_quote_values() -> None:
    quote = _fixture(500_000)
    easy = _easy(quote)
    outcome = _run_quote(quote)
    assert outcome.status == "SUCCEEDED"
    (card,) = outcome.blocks
    assert card["fees"] == {
        key: easy[key]
        for key in (
            "product",
            "sendFeeKrw",
            "telegraphicFeeKrw",
            "feeUsd",
            "fxMarginKrw",
            "totalWithdrawKrw",
            "preferentialPct",
            "usdAppliedRate",
            "notices",
        )
    }
    assert card["rateSources"] == {"usd": "KOREAEXIM", "php": "frankfurter-ecb"}
    # 기존 필드는 그대로(추가만).
    assert card["sendAmount"] == quote["sendAmount"]
    assert card["receiveAmount"] == quote["receiveAmount"]
    assert card["appliedRate"] == quote["appliedRate"]
    assert card["transferPath"] == "/transfer?recipientId=1&amount=500000"
    assert card["recipient"] == LORNA


def test_new_card_fields_have_no_korean() -> None:
    (card,) = _run_quote(_fixture(500_000)).blocks
    assert not HANGUL.search(json.dumps([card["fees"], card["rateSources"]], ensure_ascii=False))
    # 한국어가 있는 값은 하위 호환으로 둔 sourceLabel 하나뿐이다. 출처 제목(sources)은 싣지 않는다.
    rest = {k: v for k, v in card.items() if k != "appliedRate"}
    assert not HANGUL.search(json.dumps(rest, ensure_ascii=False))
    assert HANGUL.search(card["appliedRate"]["sourceLabel"])


def test_sol_fast_default_product_uses_its_usd_fee() -> None:
    quote = _fixture(500_000)
    quote["defaultProduct"] = "SOL_FAST"
    outcome = _run_quote(quote)
    (card,) = outcome.blocks
    fees = card["fees"]
    assert (fees["product"], fees["feeUsd"], fees["sendFeeKrw"], fees["telegraphicFeeKrw"]) == (
        "SOL_FAST",
        3.5,
        None,
        None,
    )
    assert (fees["fxMarginKrw"], fees["totalWithdrawKrw"], fees["usdAppliedRate"]) == (
        4681,
        499_988,
        1343.8,
    )
    content = json.loads(outcome.content)
    guide = " ".join(content["answerGuide"])
    assert "a USD 3.5 fee" in guide
    assert "telegraphic" not in guide
    assert "preferential" not in guide  # 쏠빠른 우대율 0
    assert content["deliveryType"] == "WESTERN_UNION"


def test_without_php_reference_rate_the_card_has_no_reference_amount() -> None:
    outcome = _run_quote(_without_php(_fixture(500_000)))
    assert outcome.status == "SUCCEEDED"
    (card,) = outcome.blocks
    assert (card["receiveAmount"], card["appliedRate"]) == (None, None)
    assert card["rateSources"] == {"usd": "KOREAEXIM", "php": None}
    content = json.loads(outcome.content)
    assert content["referenceAmount"] is None
    assert "Say the recipient gets USD 370.81 before intermediary bank fees." in " ".join(
        content["answerGuide"]
    )


def test_amount_too_small_has_no_card_and_gives_the_reason() -> None:
    quote = _fixture(4700)
    outcome = _run_quote(quote, {**ARGS, "amount": 4700})
    assert (outcome.status, outcome.error_code, outcome.blocks) == (
        "REJECTED",
        "QUOTE_UNAVAILABLE",
        [],
    )
    content = json.loads(outcome.content)
    assert content["unavailableReason"] == "AMOUNT_TOO_SMALL"
    assert content["reason"] == "The amount is too small: nothing is left to send after the fees."


def test_existing_request_checks_still_apply() -> None:
    quote = _fixture(500_000)
    quote["recipientId"] = 2  # 요청은 1
    outcome = _run_quote(quote)
    assert (outcome.status, outcome.error_code, outcome.blocks) == (
        "FAILED",
        "INVALID_RESPONSE",
        [],
    )


# --- LLM 입력 ---------------------------------------------------------------------------


def test_llm_input_has_fees_notices_sources_and_an_english_guide() -> None:
    outcome = _run_quote(_fixture(500_000))
    content = json.loads(outcome.content)
    assert content["fees"] == {
        "sendFeeKrw": 0,
        "telegraphicFeeKrw": 5000,
        "feeUsd": None,
        "fxMarginKrw": 1413,
        "totalWithdrawKrw": 499_998,
        "unusedKrw": None,
    }
    assert content["remittanceRate"] == {"rate": 1334.91, "rateText": "1 USD = 1,334.91 KRW"}
    assert (content["sendAmountUsd"], content["preferentialPct"], content["remittanceCount1y"]) == (
        370.81,
        70,
        23,
    )
    assert [n["code"] for n in content["notices"]] == [
        "INTERMEDIARY_FEE_MAY_APPLY",
        "LOCAL_PAYOUT_RATE_DIFFERS",
        "FOREIGN_EXCHANGE_BANK_DESIGNATION_REQUIRED",
    ]
    # 출처는 한국어 원문(title·approvalNo)과 답변에 쓸 영어 이름(sourceName)을 같이 준다.
    fee_source = next(s for s in content["sources"] if s["item"] == "telegraphicFeeKrw")
    assert fee_source["approvalNo"].startswith("준법감시인")
    assert fee_source["sourceName"] == "Shinhan's published fee table"
    # PHP 환율은 참고 금액 아래에만 둔다(appliedRate라는 이름으로 주지 않는다).
    assert "appliedRate" not in content
    assert content["referenceAmount"]["rate"]["rateText"] == "1 PHP = 20.926 KRW"
    guide = " ".join(content["answerGuide"])
    for text in (
        "KRW 500,000 is the total that leaves the user's account",
        "a KRW 5,000 telegraphic fee",
        "about KRW 1,413",
        "1 USD = 1,334.91 KRW",
        "about 23,587 PHP (reference)",
        "never call it the remittance rate",
        "intermediary bank fees may also apply",
        "the user's sample remittance history (an estimate)",
        "Never copy source titles or approval numbers",
        "real-time",
    ):
        assert text in guide, text
    assert "sending fee" not in guide  # 0원 수수료는 말하지 않는다
    assert not HANGUL.search(guide)
    # 측정(evaluate_chat) 숫자 검사 근거로 같은 값을 남긴다.
    assert outcome.measurement == {"arguments": ARGS, "content": content}


# --- 숫자 검사 --------------------------------------------------------------------------


def test_fee_numbers_are_supported_and_made_up_ones_are_not() -> None:
    content = json.loads(_run_quote(_fixture(500_000)).content)
    answer = (
        "Lorna Reyes would receive about 23,587 PHP (reference) from your KRW 500,000. "
        "KRW 499,998 leaves your account, including a KRW 5,000 telegraphic fee, and about "
        "KRW 1,413 exchange margin is built into the rate of 1 USD = 1,334.91 KRW "
        "(USD 370.81 sent, 70% preferential rate from 23 sample remittances)."
    )
    assert check_answer(answer, [content], []).unsupported == []
    made_up = "That is KRW 6,415 less than 1 USD = 1,340 KRW would give."
    assert check_answer(made_up, [content], []).unsupported == ["KRW 6,415", "1,340 KRW"]

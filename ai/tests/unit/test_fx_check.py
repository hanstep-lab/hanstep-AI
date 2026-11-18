"""scripts/_fx_check.py: 환율 진단 답변의 기간별 방향·예측·권유 문장 판정(#91). API 호출 없음."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import _fx_check as FX  # noqa: E402

pytestmark = pytest.mark.unit


def _content(currency: str, directions: dict[str, str], insufficient: tuple[str, ...] = ()) -> Any:
    return {
        "currencies": [
            {
                "currency": currency,
                "periodAverages": [
                    {"period": p, "currentVsAverage": d} for p, d in directions.items()
                ],
                "insufficientDataPeriods": list(insufficient),
            }
        ]
    }


PHP_LOWER = _content("PHP", {"1M": "LOWER", "3M": "LOWER", "6M": "LOWER", "1Y": "LOWER"})
USD = _content("USD", {"1M": "SAME", "3M": "LOWER", "6M": "LOWER", "1Y": "LOWER"})


def _results(check: dict[str, Any]) -> list[tuple[str | None, str, str]]:
    return [(m["currency"], m["period"], m["result"]) for m in check["mentions"]]


def test_one_direction_word_covers_a_list_of_periods() -> None:
    answer = (
        "1 PHP = 20.984 KRW. It is lower than the 1-month average of 21.0393 KRW "
        "(22 business days), the 3-month average of 21.5431 KRW (67 business days)."
    )
    check = FX.direction_check(answer, [PHP_LOWER])
    assert _results(check) == [("PHP", "1M", "match"), ("PHP", "3M", "match")]
    assert check["mismatches"] == 0


def test_average_as_subject_is_flipped() -> None:
    answer = (
        "The PHP rate is 20.984 KRW. "
        "The 3-month and 6-month averages also sit above the current rate."
    )
    check = FX.direction_check(answer, [PHP_LOWER])
    assert [(m["period"], m["said"]) for m in check["mentions"]] == [
        ("3M", "LOWER"),
        ("6M", "LOWER"),
    ]
    assert check["mismatches"] == 0


def test_same_and_lower_in_one_sentence() -> None:
    answer = (
        "USD 1 = 1406.51 KRW. That is about the same as its 1-month average of 1407.7735 KRW and "
        "lower than its 3-month average of 1429.6566 KRW."
    )
    check = FX.direction_check(answer, [USD])
    assert [(m["period"], m["said"], m["result"]) for m in check["mentions"]] == [
        ("1M", "SAME", "match"),
        ("3M", "LOWER", "match"),
    ]


def test_wrong_direction_is_a_mismatch() -> None:
    check = FX.direction_check("The peso is higher than its 3-month average.", [PHP_LOWER])
    assert check["mismatches"] == 1
    assert check["mentions"][0]["said"] == "HIGHER" and check["mentions"][0]["expected"] == "LOWER"


def test_currency_is_the_nearest_one_before_the_period() -> None:
    answer = (
        "USD: lower than its 3-month average of 1429.6566 KRW. "
        "PHP: higher than its 1-month average of 21.0393 KRW."
    )
    check = FX.direction_check(answer, [USD, PHP_LOWER])
    assert _results(check) == [("USD", "3M", "match"), ("PHP", "1M", "mismatch")]
    assert check["mismatches"] == 1


def test_direction_for_a_period_without_data_counts_as_a_mismatch() -> None:
    content = _content("VND", {"1M": "LOWER"}, insufficient=("3M",))
    check = FX.direction_check("The VND rate is lower than its 3-month average.", [content])
    assert _results(check) == [("VND", "3M", "insufficientData")]
    assert check["mismatches"] == 1


def test_direction_in_the_next_sentence() -> None:
    answer = (
        "1 PHP = 20.984 KRW, compared with a 1-year average of 23.8157 KRW over 255 business days. "
        "That is 2.8317 KRW lower, or 11.89% below the average."
    )
    check = FX.direction_check(answer, [PHP_LOWER])
    assert _results(check) == [("PHP", "1Y", "match")]


def test_no_direction_is_counted_apart() -> None:
    answer = (
        "PHP 1 = 20.984 KRW, compared with averages of 21.0393 KRW (1 month, 22 business days)."
    )
    check = FX.direction_check(answer, [PHP_LOWER])
    # "1 month"는 기간 표현이 아니라("1-month"·"one month"만 셈) 언급이 없다.
    assert check["mismatches"] == 0
    answer = "PHP 1 = 20.984 KRW, compared with the 6-month average of 22.9364 KRW."
    check = FX.direction_check(answer, [PHP_LOWER])
    assert (check["noDirection"], check["mismatches"]) == (1, 0)


def test_advice_sentences_skip_negations() -> None:
    answer = (
        "I can't predict whether the rate will rise. This is not a forecast. "
        "The peso will likely get cheaper next week, so you should wait."
    )
    assert FX.advice_sentences(answer) == [
        "The peso will likely get cheaper next week, so you should wait."
    ]

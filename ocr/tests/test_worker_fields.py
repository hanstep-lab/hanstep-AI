"""엔진 결과 → OCR 결과 보고 본문 변환, 등록번호 마스킹 재검사, 필수 항목 판정."""

from __future__ import annotations

import json
from typing import Any

import pytest
from worker_fakes import FULL_REGISTRATION_NO, MASKED_REGISTRATION_NO, engine_result

from worker.fields import FIELD_KEYS, build_ocr_result
from worker.handler import JobFailure

pytestmark = pytest.mark.unit


def by_key(raw: Any) -> dict[str, dict[str, Any]]:
    return {f["fieldKey"]: f for f in build_ocr_result(42, raw).to_json()["fields"]}


def test_converts_engine_fields_to_callback_body() -> None:
    body = build_ocr_result(42, engine_result()).to_json()

    assert body == {
        "documentId": 42,
        "modelVersion": "ppocrv5-ko-mdet-mrec",
        "latencyMs": 640,
        "side": "FRONT",
        "fields": [
            {
                "fieldKey": "nameEn",
                "value": "MARK ANTHONY REYES",
                "confidence": 0.97,
                "isMasked": False,
            },
            {"fieldKey": "nationality", "value": "PHL", "confidence": 0.97, "isMasked": False},
            {"fieldKey": "birthDate", "value": "1995-08-15", "confidence": 0.97, "isMasked": False},
            {"fieldKey": "visaType", "value": "E-9", "confidence": 0.97, "isMasked": False},
            {"fieldKey": "stayExpiry", "value": None, "confidence": None, "isMasked": False},
            {
                "fieldKey": "registrationNo",
                "value": MASKED_REGISTRATION_NO,
                "confidence": 0.97,
                "isMasked": True,
            },
        ],
    }


def test_field_key_table_matches_backend_arc_field_keys() -> None:
    assert FIELD_KEYS == {
        "NAME_EN": "nameEn",
        "NATIONALITY": "nationality",
        "BIRTH_DATE": "birthDate",
        "VISA_TYPE": "visaType",
        "STAY_EXPIRES_ON": "stayExpiry",
        "REGISTRATION_NO": "registrationNo",
    }


def test_missing_and_unknown_engine_fields_still_send_six_keys() -> None:
    raw = engine_result()
    raw["fields"] = [f for f in raw["fields"] if f["name"] != "BIRTH_DATE"]
    raw["fields"].append({"name": "NAME_KO", "value": "샘플", "confidence": 0.9})

    fields = by_key(raw)

    assert list(fields) == list(FIELD_KEYS.values())
    assert fields["birthDate"] == {
        "fieldKey": "birthDate",
        "value": None,
        "confidence": None,
        "isMasked": False,
    }


def test_duplicate_engine_field_uses_first_value() -> None:
    raw = engine_result()
    raw["fields"].append({"name": "VISA_TYPE", "value": "F-2", "confidence": 0.99})
    assert by_key(raw)["visaType"]["value"] == "E-9"


# ---------- 등록번호 마스킹 재검사 ----------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(MASKED_REGISTRATION_NO, MASKED_REGISTRATION_NO, id="masked"),
        pytest.param(FULL_REGISTRATION_NO, None, id="full-number"),
        pytest.param("9508155123456", None, id="full-number-no-hyphen"),
        pytest.param("950815-5*****", None, id="short-mask"),
        pytest.param("950815-*******", None, id="gender-digit-masked"),
        pytest.param("950815 5******", None, id="space-instead-of-hyphen"),
        pytest.param("950815-5******0", None, id="trailing-digit"),
        pytest.param(None, None, id="not-read"),
    ],
)
def test_registration_number_must_be_masked(value: str | None, expected: str | None) -> None:
    field = by_key(engine_result(REGISTRATION_NO=value))["registrationNo"]

    assert field["value"] == expected
    assert field["isMasked"] is True
    # 값을 버리면 신뢰도도 보내지 않는다
    assert field["confidence"] == (0.97 if expected else None)


def test_full_number_in_any_field_is_dropped() -> None:
    raw = engine_result(
        NAME_EN=f"REYES {FULL_REGISTRATION_NO}", REGISTRATION_NO=FULL_REGISTRATION_NO
    )

    body = json.dumps(build_ocr_result(42, raw).to_json())

    assert "5123456" not in body
    assert "9508155123456" not in body.replace("-", "")


@pytest.mark.parametrize("value", ["", "   ", "A" * 256])
def test_blank_or_too_long_value_is_sent_as_missing(value: str) -> None:
    field = by_key(engine_result(NAME_EN=value))["nameEn"]
    assert field["value"] is None and field["confidence"] is None


# ---------- 필수 항목 ----------


def test_all_required_fields_missing_is_ocr_failed() -> None:
    raw = engine_result(NAME_EN=None, NATIONALITY=None, VISA_TYPE=None, REGISTRATION_NO=None)

    with pytest.raises(JobFailure) as caught:
        build_ocr_result(42, raw)

    assert caught.value.code == "OCR_FAILED"
    assert caught.value.transient is False


def test_required_fields_missing_after_mask_check_is_ocr_failed() -> None:
    # 등록번호만 읽었는데 마스킹 형식이 아니면 남는 필수 항목이 없다
    raw = engine_result(
        NAME_EN=None, NATIONALITY=None, VISA_TYPE=None, REGISTRATION_NO=FULL_REGISTRATION_NO
    )

    with pytest.raises(JobFailure) as caught:
        build_ocr_result(42, raw)

    assert caught.value.code == "OCR_FAILED"


@pytest.mark.parametrize("kept", ["NAME_EN", "NATIONALITY", "VISA_TYPE", "REGISTRATION_NO"])
def test_one_required_field_is_enough(kept: str) -> None:
    values: dict[str, Any] = {
        name: None for name in ("NAME_EN", "NATIONALITY", "VISA_TYPE", "REGISTRATION_NO")
    }
    values.pop(kept)
    fields = by_key(engine_result(**values))
    assert fields[FIELD_KEYS[kept]]["value"] is not None


# ---------- 엔진 출력 검증 ----------


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(None, id="none"),
        pytest.param({"latencyMs": 1, "fields": []}, id="no-model-version"),
        pytest.param({"modelVersion": "m", "latencyMs": -1, "fields": []}, id="negative-latency"),
        pytest.param(
            {
                "modelVersion": "m",
                "latencyMs": 1,
                "fields": [
                    {"name": "REGISTRATION_NO", "value": FULL_REGISTRATION_NO, "confidence": 1.5}
                ],
            },
            id="confidence-out-of-range",
        ),
    ],
)
def test_invalid_engine_output_is_ocr_failed_without_values(raw: Any) -> None:
    with pytest.raises(JobFailure) as caught:
        build_ocr_result(42, raw)

    assert caught.value.code == "OCR_FAILED"
    assert caught.value.transient is False
    assert "5123456" not in caught.value.reason
    assert caught.value.__cause__ is None and caught.value.__suppress_context__

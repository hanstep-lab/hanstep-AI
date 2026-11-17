"""엔진 결과 → OCR 결과 보고 본문(hanstep-backend OcrResultRequest).

엔진(``pipeline.engine.recognize``) 출력은 경계에서 검증한다. 오류에는 필드 이름만 싣고 값은
싣지 않는다(검증 오류 문자열에는 입력값이 들어간다). 등록번호는 엔진이 이미 마스킹하지만
여기서 형식을 다시 확인하고, 형식이 아니면 값을 버린다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

from worker.handler import JobFailure

SIDE = "FRONT"

# 엔진 필드 이름 → BE fieldKey(ArcFieldKey). 이 순서로 6개를 한 번씩 보낸다.
FIELD_KEYS: dict[str, str] = {
    "NAME_EN": "nameEn",
    "NATIONALITY": "nationality",
    "BIRTH_DATE": "birthDate",
    "VISA_TYPE": "visaType",
    "STAY_EXPIRES_ON": "stayExpiry",
    "REGISTRATION_NO": "registrationNo",
}
MASKED_KEYS = frozenset({"registrationNo"})
# 모두 null이면 사용자 확인으로 넘길 것이 없어 OCR_FAILED로 끝낸다.
REQUIRED_KEYS = ("nameEn", "nationality", "visaType", "registrationNo")

# BE DocumentInternalService.MASKED_REGISTRATION_NO와 같은 형식.
MASKED_REGISTRATION_NO = re.compile(r"^[0-9]{6}-[0-9]\*{6}$")
# 마스킹 전 등록번호 모양(엔진 REG_NO와 같은 구분자). 어느 항목에서든 보이면 값을 버린다.
_FULL_REGISTRATION_NO = re.compile(r"\d{6}\s*[-–—~]?\s*\d{7}")
# BE OcrFieldResult.value @Size(max = 255), OcrResultRequest.modelVersion @Size(max = 60).
MAX_VALUE_LENGTH = 255


class EngineField(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    value: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)


class EngineResult(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, frozen=True, extra="ignore")

    model_version: str = Field(min_length=1, max_length=60)
    latency_ms: int = Field(ge=0)
    fields: list[EngineField]


@dataclass(frozen=True)
class OcrResult:
    document_id: int
    model_version: str
    latency_ms: int
    fields: tuple[dict[str, Any], ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "documentId": self.document_id,
            "modelVersion": self.model_version,
            "latencyMs": self.latency_ms,
            "side": SIDE,
            "fields": [dict(field) for field in self.fields],
        }


def parse_engine_result(raw: object) -> EngineResult:
    try:
        return EngineResult.model_validate(raw)
    except ValidationError as exc:
        locations = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
        raise JobFailure(
            "OCR_FAILED", transient=False, reason=f"engine output invalid: {locations}"
        ) from None


def _clean_value(key: str, value: str | None) -> str | None:
    if value is None or not value.strip() or len(value) > MAX_VALUE_LENGTH:
        return None
    if key in MASKED_KEYS:
        # 마스킹 형식(######-#******)이 아니면 전체 번호일 수 있으므로 보내지 않는다
        return value if MASKED_REGISTRATION_NO.fullmatch(value) else None
    if _FULL_REGISTRATION_NO.search(value):
        return None
    return value


def build_ocr_result(document_id: int, raw: Mapping[str, Any] | object) -> OcrResult:
    """엔진 결과를 보고 본문으로 바꾼다. 필수 항목을 하나도 못 읽었으면 JobFailure(OCR_FAILED)."""
    result = parse_engine_result(raw)
    by_name: dict[str, EngineField] = {}
    for field in result.fields:
        by_name.setdefault(field.name, field)  # 같은 이름이 또 오면 처음 값을 쓴다

    fields: list[dict[str, Any]] = []
    for name, key in FIELD_KEYS.items():
        found = by_name.get(name)
        value = _clean_value(key, found.value if found else None)
        confidence = found.confidence if found is not None and value is not None else None
        fields.append(
            {
                "fieldKey": key,
                "value": value,
                "confidence": confidence,
                "isMasked": key in MASKED_KEYS,
            }
        )

    if all(f["value"] is None for f in fields if f["fieldKey"] in REQUIRED_KEYS):
        raise JobFailure("OCR_FAILED", transient=False, reason="no required field recognized")
    return OcrResult(
        document_id=document_id,
        model_version=result.model_version,
        latency_ms=result.latency_ms,
        fields=tuple(fields),
    )

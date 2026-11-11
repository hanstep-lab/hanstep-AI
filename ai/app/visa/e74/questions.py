"""감점·제외 자기신고 항목: JSON 이름 → 값 읽기, 재질의 문구.

모두 "최근 10년" 기준이라 날짜 없이 질문 문구로 기간을 정한다(정의서 0장).
"""

from __future__ import annotations

from app.visa.e74.results import MissingInput
from app.visa.e74.schemas import E74Profile, LegalDeclarationIn

DECLARATION_QUESTIONS: dict[str, str] = {
    "finesUnder1mCount": "최근 10년 안에 벌금 100만원 미만의 형을 받은 횟수",
    "hasFine1mOrMore": "최근 10년 안에 벌금 100만원 이상의 형을 받은 적이 있는지",
    "taxStayRestrictionCount": "최근 10년 안에 세금 체납 때문에 체류허가가 제한된 횟수",
    "immigrationViolationCount": (
        "최근 10년 안에 출입국관리법 위반으로 처분(과태료 포함)을 받은 횟수"
    ),
    "hasOverstay3mOrMore": "최근 10년 안에 3개월 이상 불법체류한 적이 있는지",
    "hasCurrentTaxArrears": "지금 내지 않은 세금(체납)이 있는지",
}

# JSON 이름(별칭) → 모델 속성 이름. 숫자가 든 이름은 별칭을 직접 지정했으므로 모델에서 읽는다.
_ATTRIBUTES: dict[str, str] = {
    str(field.alias): name for name, field in LegalDeclarationIn.model_fields.items()
}


def declared_value(profile: E74Profile, field: str) -> int | bool | None:
    """자기신고 값. 신고 자체가 없거나 그 항목이 비어 있으면 None."""
    declaration = profile.legal_declaration
    if declaration is None:
        return None
    value: int | bool | None = getattr(declaration, _ATTRIBUTES[field])
    return value


def declaration_missing(field: str) -> MissingInput:
    return MissingInput(field=f"legalDeclaration.{field}", question=DECLARATION_QUESTIONS[field])

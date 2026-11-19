"""재질의 문구(한국어·영어)와 감점·제외 자기신고 값 읽기.

- 문구는 QUESTIONS 한곳에 질문 키별로 둔다. 엔진은 missing_input()으로만 MissingInput을 만든다.
- 질문 키는 field 경로에서 id를 뺀 이름이다. 같은 field에 문구가 둘이면(employmentHistories)
  뒤에 뜻을 붙여 나눈다.
- 영어 문구: 한국어와 뜻이 같은 쉬운 영어 한 문장. 감점·제외 항목은 사실만 묻고, 비난하는 투나
  법률 판단처럼 읽히는 말을 쓰지 않는다. 금액은 KRW로 쓴다.
- 감점·제외 항목은 모두 "최근 10년" 기준이라 날짜 없이 질문 문구로 기간을 정한다(정의서 0장).
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass
from functools import cache

from app.visa.e74.results import MissingInput
from app.visa.e74.schemas import E74Profile, LegalDeclarationIn


@dataclass(frozen=True)
class Question:
    """질문 하나의 한국어·영어 문구. {이름} 자리에는 같은 값이 들어간다."""

    ko: str
    en: str


QUESTIONS: dict[str, Question] = {
    # --- 체류 정보(visaProfile) ---
    "visaProfile.birthDate": Question("생년월일", "What is your date of birth?"),
    "visaProfile.currentStayStartedOn": Question(
        "현재 체류를 시작한 날(이번에 한국에 입국한 날)",
        "When did your current stay start (the date you last entered Korea)?",
    ),
    "visaProfile.priorStayMonths": Question(
        "이번 입국 전, 최근 10년 안에 E-9·E-10·H-2로 한국에 있었던 기간 합계(개월)",
        "Before this entry, how many months in total did you stay in Korea on an E-9, E-10"
        " or H-2 visa in the last 10 years?",
    ),
    "visaProfile.visaTypeCode": Question(
        "현재 체류자격(예: E-9)", "What is your current visa type (for example, E-9)?"
    ),
    # --- 소득(incomeRecords) ---
    "incomeRecords": Question(
        "{year}년 연간 소득(세무서 소득금액증명원 기준 금액)",
        "What was your annual income in {year}"
        " (the amount on your income certificate from the tax office)?",
    ),
    # --- 근무 이력(employmentHistories) ---
    "employmentHistories": Question(
        "근무 이력(사업장, 업종, 근무 시작일·종료일)",
        "What is your work history (workplace, industry, start date and end date)?",
    ),
    "employmentHistories.industry": Question(
        "현재 근무처 업종(농·축산업, 어업·내항상선은 소득 기준이 낮음)",
        "What industry is your current workplace in"
        " (farming, livestock, fishing and coastal shipping have a lower income standard)?",
    ),
    "employmentHistories.isDepopulationArea": Question(
        "{start}~{end} 근무지가 인구감소지역 또는 읍·면 지역인지",
        "Was your workplace from {start} to {end} in a depopulation area"
        " or a town or township (eup or myeon)?",
    ),
    "employmentHistories.isDepopulationArea.current": Question(
        "{start}~현재 근무지가 인구감소지역 또는 읍·면 지역인지",
        "Is your workplace since {start} in a depopulation area"
        " or a town or township (eup or myeon)?",
    ),
    "employmentHistories.leftWithoutFault": Question(
        "직전 근무처를 휴·폐업, 임금체불, 폭언·폭행 등 본인 잘못이 아닌 이유로 옮겼는지",
        "Did you leave your last workplace for a reason that was not your fault, such as"
        " the business closing, unpaid wages, or verbal or physical abuse?",
    ),
    "employmentHistories.contractEndOn": Question(
        "현재 회사와 맺은(맺을) E-7-4 고용계약의 종료일",
        "When does the E-7-4 employment contract you signed (or will sign)"
        " with your current company end?",
    ),
    "employmentHistories.contractAnnualSalary": Question(
        "E-7-4 고용계약 연봉(기본급·수당을 포함한 1년 총액)",
        "What is the annual salary in your E-7-4 employment contract"
        " (the total for one year, including base pay and allowances)?",
    ),
    # --- 증빙(visaEvidences) ---
    "visaEvidences.koreanAbility": Question(
        "한국어 능력(TOPIK 급수, 사회통합프로그램 이수 단계, 사전평가 점수)",
        "What is your Korean level (TOPIK level, completed KIIP stage or KIIP pre-test score)?",
    ),
    "visaEvidences.preTestScore": Question(
        "사회통합프로그램 사전평가 점수",
        "What was your score on the KIIP (Korea Immigration and Integration Program) pre-test?",
    ),
    "visaEvidences.preTestAssignedStage": Question(
        "사전평가 후 배정받은 단계", "Which KIIP stage were you placed in after the pre-test?"
    ),
    "visaEvidences.preTestIssuedOn": Question(
        "사전평가 결과일", "On what date did you get your KIIP pre-test result?"
    ),
    "visaEvidences.certificateName": Question(
        "자격증 이름(예: 용접기능사)",
        "What is the name of your certificate (for example, Craftsman Welding)?",
    ),
    "visaEvidences.certificateRelatedToCurrentJob": Question(
        "자격증 '{name}'이 지금 하는 일과 관련 있는지",
        "Is your certificate '{name}' related to the work you do now?",
    ),
    "visaEvidences.degreeLevel": Question(
        "국내 학위 과정(전문학사, 학사, 석사, 박사)",
        "What degree did you earn in Korea (associate, bachelor's, master's or doctorate)?",
    ),
    "visaEvidences.driverLicenseType": Question(
        "운전면허 종류(예: 2종 보통)",
        "What type of driver's license do you have (for example, Class 2 Ordinary)?",
    ),
    "visaEvidences.employerRecommendation": Question(
        "현재 회사 대표의 고용기업 추천서를 받았는지(받을 수 있는지)",
        "Did you get (or can you get) an employer recommendation letter"
        " from the head of your current company?",
    ),
    # --- 감점·제외 자기신고(legalDeclaration) ---
    "legalDeclaration.finesUnder1mCount": Question(
        "최근 10년 안에 벌금 100만원 미만의 형을 받은 횟수",
        "In the last 10 years, how many times did you receive a fine under KRW 1,000,000?",
    ),
    "legalDeclaration.hasFine1mOrMore": Question(
        "최근 10년 안에 벌금 100만원 이상의 형을 받은 적이 있는지",
        "In the last 10 years, did you receive a fine of KRW 1,000,000 or more?",
    ),
    "legalDeclaration.taxStayRestrictionCount": Question(
        "최근 10년 안에 세금 체납 때문에 체류허가가 제한된 횟수",
        "In the last 10 years, how many times was your stay permission limited"
        " because of unpaid taxes?",
    ),
    "legalDeclaration.immigrationViolationCount": Question(
        "최근 10년 안에 출입국관리법 위반으로 처분(과태료 포함)을 받은 횟수",
        "In the last 10 years, how many times did you receive a penalty under"
        " the Immigration Act, including administrative fines?",
    ),
    "legalDeclaration.hasOverstay3mOrMore": Question(
        "최근 10년 안에 3개월 이상 불법체류한 적이 있는지",
        "In the last 10 years, did you stay in Korea for 3 months or more"
        " after your permitted stay ended?",
    ),
    "legalDeclaration.hasCurrentTaxArrears": Question(
        "지금 내지 않은 세금(체납)이 있는지", "Do you have any unpaid taxes right now?"
    ),
}


def missing_input(key: str, field: str, **params: object) -> MissingInput:
    """QUESTIONS[key] 문구로 누락 입력을 만든다. params는 문구의 {이름} 자리 값이다."""
    question = QUESTIONS[key]
    return MissingInput(
        field=field,
        question=question.ko.format(**params),
        question_en=question.en.format(**params),
    )


def placeholders(text: str) -> set[str]:
    """문구에 든 {이름} 자리."""
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


@cache
def _korean_patterns() -> tuple[tuple[re.Pattern[str], Question], ...]:
    # {이름} 자리가 적은 문구부터 맞춘다. "{start}~{end} …"가 "{start}~현재 …"를 먼저 잡지 않게.
    patterns = []
    for question in sorted(QUESTIONS.values(), key=lambda q: len(placeholders(q.ko))):
        parts = []
        for literal, name, _, _ in string.Formatter().parse(question.ko):
            parts.append(re.escape(literal))
            if name:
                parts.append(f"(?P<{name}>.+?)")
        patterns.append((re.compile("".join(parts)), question))
    return tuple(patterns)


def english_question(korean: str) -> str | None:
    """한국어 문구 → 같은 질문의 영어 문구. questionEn이 없던 저장 결과(#103 전)에 쓴다.

    QUESTIONS에 없는 문구면 None.
    """
    for pattern, question in _korean_patterns():
        match = pattern.fullmatch(korean)
        if match:
            return question.en.format(**match.groupdict())
    return None


# --- 감점·제외 자기신고 값 ------------------------------------------------------------

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
    key = f"legalDeclaration.{field}"
    return missing_input(key, field=key)

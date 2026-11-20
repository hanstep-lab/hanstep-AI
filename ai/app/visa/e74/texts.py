"""개선 방안·규칙 설명·서류 이름·검토 메모의 한국어·영어 문구(#108).

- 문구는 이 파일 한곳에 key별로 둔다. 엔진은 한국어(`description`, `documents`, `reviewNotes`)를
  예전과 글자 하나 다르지 않게 만들고, 개선 방안에는 영어(`descriptionEn`, `documentsEn`)를
  같이 싣는다.
- LLM에 주는 도구 결과(app.visa.tools)는 사용자 언어로 고른다: ko면 한국어, 그 밖이면 영어.
  영어가 없던 저장 결과(#108 전, 재사용된 평가)는 한국어 문구로 같은 문구의 영어를 찾는다
  (`english_improvement`, `english_documents`).
- LLM용 한국어(`llm_ko`): 엔진 한국어에 내부 표기(Q8·Q9 같은 확인 번호, "(붙임5)" 같은 양식 번호,
  "검토 필요")가 있으면 그것을 뺀 문구를 따로 둔다. 없으면 엔진 한국어를 그대로 쓴다.
- 검토 메모(사람 확인용)는 두 언어 모두 LLM에 주지 않는다. 메모 key가 곧 LLM 안내 코드이고, 사용자
  언어의 안내 문구(ko는 `llm_ko`, 그 밖은 `en`)를 준다(`review_notice`).
- 규칙 설명·서류 이름의 한국어는 규칙 JSON(rule_sets/*.json)이 원본이다. 여기 한국어는 영어를 찾는
  열쇠이고, 규칙 JSON과 같은지는 테스트가 확인한다.
- 영어: 한국어와 뜻이 같은 쉬운 영어. 금액은 KRW로 쓴다. 내부 표기는 옮기지 않는다.
"""

from __future__ import annotations

import re
import string
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import cache

HANGUL = re.compile(r"[가-힣]")


@dataclass(frozen=True)
class Text:
    """문구 하나의 한국어·영어. {이름} 자리에는 같은 값이 들어간다.

    사전 항목은 둘 다 있다. 값을 넣어 만든 문구는 영어가 없는 값이 섞이면 en이 None이다.
    llm_ko는 LLM에 줄 한국어로, 엔진 한국어(ko)에 내부 표기가 있을 때만 둔다.
    """

    ko: str
    en: str | None
    llm_ko: str | None = None


# --- 개선 방안(improvements[].description). key는 messageCode.
# 같은 코드의 다른 문구는 key 뒤에 뜻을 붙인다(.later 등).
IMPROVEMENTS: dict[str, Text] = {
    "E74_IMPROVE_STAY_WAIT": Text(
        "체류 {years}년 요건은 지금처럼 계속 체류하면 {date}부터 충족",
        "If you keep staying as you do now, you meet the {years}-year stay requirement from {date}",
    ),
    "E74_IMPROVE_CURRENT_EMPLOYMENT": Text(
        "{visa_types} 자격으로 현재 근무처에서 일하고 있어야 함",
        "You must be working at your current workplace with {visa_types} status",
    ),
    "E74_IMPROVE_CONTRACT_SALARY": Text(
        "E-7-4 고용계약 연봉을 {required}원 이상으로(현재 {salary}원, {shortfall}원 부족)",
        "The annual salary in the E-7-4 employment contract must be KRW {required} or more"
        " (now KRW {salary}, KRW {shortfall} short)",
    ),
    "E74_IMPROVE_CONTRACT_PERIOD": Text(
        "E-7-4 고용계약 기간을 {required_end}까지 이상으로(현재 {contract_end}까지)",
        "The E-7-4 employment contract must run until {required_end} or later"
        " (now until {contract_end})",
    ),
    "E74_IMPROVE_NEEDS_CURRENT_JOB": Text(
        "현재 근무처가 있어야 고용기업 추천을 받을 수 있음",
        "You need a current workplace to get an employer recommendation",
    ),
    "E74_IMPROVE_EMPLOYER_TENURE_WAIT": Text(
        "현재 근무처 {months}개월 근무가 {date} 채워지면 고용기업 추천을 받을 수 있음",
        "You can get an employer recommendation once you have worked {months} months"
        " at your current workplace, on {date}",
    ),
    "E74_IMPROVE_EMPLOYER_TENURE_WAIT.later": Text(
        "현재 근무처 {months}개월 근무가 나중에 채워지면 고용기업 추천을 받을 수 있음",
        "You can get an employer recommendation once you have worked {months} months"
        " at your current workplace",
    ),
    "E74_IMPROVE_MIN_INCOME": Text(
        "최근 2년 평균소득이 {required}원 이상이어야 함(현재 {average}원, {shortfall}원 부족)",
        "Your average income over the last 2 years must be KRW {required} or more"
        " (now KRW {average}, KRW {shortfall} short)",
    ),
    "E74_IMPROVE_MIN_KOREAN": Text(
        "한국어: TOPIK {topik}급, 사회통합프로그램 {kiip}단계 이수, 또는 사전평가 {pre_test}점 이상"
        "({stage}단계 이상 배정)",
        "Korean: TOPIK level {topik}, KIIP stage {kiip} completed, or a KIIP pre-test score of"
        " {pre_test} or more (placed in stage {stage} or higher)",
    ),
    "E74_IMPROVE_MIN_KOREAN.deferral": Text(
        "한국어: TOPIK {topik}급, 사회통합프로그램 {kiip}단계 이수, 또는 사전평가 {pre_test}점 이상"
        "({stage}단계 이상 배정). {until}까지는 한국어 외 점수가 {min_total}점 이상이면 유예 가능",
        "Korean: TOPIK level {topik}, KIIP stage {kiip} completed, or a KIIP pre-test score of"
        " {pre_test} or more (placed in stage {stage} or higher). Until {until}, this can be"
        " deferred if your score without Korean is {min_total} points or more",
    ),
    "E74_IMPROVE_TOTAL_SCORE": Text(
        "총점이 {needed}점 부족(기준 {required}점). 아래 방안으로 채울 수 있음",
        "Your total score is {needed} points short (required: {required} points)."
        " The other improvements in this list can make up the difference",
    ),
    "E74_IMPROVE_RAISE_INCOME": Text(
        "최근 2년 평균소득을 {needed}원 이상으로 올리면 +{gain}점(현재 {average}원)",
        "Raise your average income over the last 2 years to KRW {needed} or more:"
        " +{gain} points (now KRW {average})",
    ),
    "E74_IMPROVE_RAISE_KOREAN": Text(
        "한국어를 TOPIK {topik}급, 사회통합프로그램 {kiip}단계, 또는 사전평가 {pre_test}점 이상으로"
        " 올리면 +{gain}점",
        "Raise your Korean to TOPIK level {topik}, KIIP stage {kiip}, or a KIIP pre-test score of"
        " {pre_test} or more: +{gain} points",
    ),
    "E74_IMPROVE_OBTAIN_RECOMMENDATION": Text(
        "{rule} 받으면 +{points}점",
        "Get this for +{points} points: {rule}",
    ),
    "E74_IMPROVE_OBTAIN_RECOMMENDATION.group": Text(
        "중앙부처 또는 광역지자체 추천을 받으면 +{points}점(둘 중 하나만 인정)",
        "Get a recommendation from a central government ministry or a provincial government:"
        " +{points} points (only one of the two counts)",
    ),
    "E74_IMPROVE_TENURE_WAIT": Text(
        "현재 근무처에서 계속 일하면 {date}부터 3년 근속 +{points}점",
        "If you keep working at your current workplace, you get +{points} points"
        " for 3 years of service from {date}",
    ),
    "E74_IMPROVE_REGION_WAIT": Text(
        "지금 근무지(인구감소지역·읍면)에서 계속 일하면 {date}부터 +{points}점",
        "If you keep working at your current workplace (a depopulation area or a town or"
        " township), you get +{points} points from {date}",
    ),
    "E74_IMPROVE_OBTAIN_QUALIFICATION": Text(
        "지금 하는 일과 관련된 국내 {grades} 자격증, "
        "또는 국내 {degree} 이상 학위를 받으면 +{points}점",
        "Get a Korean national certificate ({grades}) related to your current job,"
        " or a Korean {degree} or higher: +{points} points",
    ),
    "E74_IMPROVE_OBTAIN_DRIVER_LICENSE": Text(
        "국내 운전면허(제2종 보통 이상)를 따면 +{points}점",
        "Get a Korean driver's license (Class 2 Ordinary or higher): +{points} points",
    ),
    "E74_IMPROVE_SUBMIT_REQUIREMENT_EVIDENCE": Text(
        "{rule}: 증빙 서류를 내면 요건이 확정됨",
        "{rule}: submit the documents to confirm this requirement",
    ),
    "E74_IMPROVE_SUBMIT_EVIDENCE": Text(
        "{rule}: 증빙 서류를 내면 +{points}점 확정",
        "{rule}: submit the documents to confirm +{points} points",
    ),
}

# --- 규칙 설명(개선 방안의 {rule} 자리). key는 ruleCode.
# 개선 방안에 나올 수 있는 범주(점수·가점·요건)만 둔다.
RULES: dict[str, Text] = {
    "INCOME_25_30": Text(
        "최근 2년 연간 평균소득 2,500만원 이상 3,000만원 미만",
        "Average annual income over the last 2 years of KRW 25,000,000 or more"
        " and under KRW 30,000,000",
    ),
    "INCOME_30_35": Text(
        "최근 2년 연간 평균소득 3,000만원 이상 3,500만원 미만",
        "Average annual income over the last 2 years of KRW 30,000,000 or more"
        " and under KRW 35,000,000",
    ),
    "INCOME_35_40": Text(
        "최근 2년 연간 평균소득 3,500만원 이상 4,000만원 미만",
        "Average annual income over the last 2 years of KRW 35,000,000 or more"
        " and under KRW 40,000,000",
    ),
    "INCOME_40_45": Text(
        "최근 2년 연간 평균소득 4,000만원 이상 4,500만원 미만",
        "Average annual income over the last 2 years of KRW 40,000,000 or more"
        " and under KRW 45,000,000",
    ),
    "INCOME_45_50": Text(
        "최근 2년 연간 평균소득 4,500만원 이상 5,000만원 미만",
        "Average annual income over the last 2 years of KRW 45,000,000 or more"
        " and under KRW 50,000,000",
    ),
    "INCOME_50_PLUS": Text(
        "최근 2년 연간 평균소득 5,000만원 이상",
        "Average annual income over the last 2 years of KRW 50,000,000 or more",
    ),
    "INCOME_24_25_AGRI_FISHERY": Text(
        "농·축산업, 어업·내항상선 종사자 평균소득 2,400만원 이상 2,500만원 미만",
        "Average income of KRW 24,000,000 or more and under KRW 25,000,000"
        " for workers in farming, livestock, fishing or coastal shipping",
    ),
    "KOREAN_L2": Text(
        "TOPIK 2급, 사회통합프로그램 2단계 이수, 사전평가 41~60점(3단계 이상 배정)",
        "TOPIK level 2, KIIP stage 2 completed, or a KIIP pre-test score of 41 to 60"
        " (placed in stage 3 or higher)",
    ),
    "KOREAN_L3": Text(
        "TOPIK 3급, 사회통합프로그램 3단계 이수, 사전평가 61~80점",
        "TOPIK level 3, KIIP stage 3 completed, or a KIIP pre-test score of 61 to 80",
    ),
    "KOREAN_L4_PLUS": Text(
        "TOPIK 4급 이상, 사회통합프로그램 4단계 이상 이수, 사전평가 81점 이상",
        "TOPIK level 4 or higher, KIIP stage 4 or higher completed,"
        " or a KIIP pre-test score of 81 or more",
    ),
    "AGE_19_26": Text("신청일 기준 만 19~26세", "Age 19 to 26 on the application date"),
    "AGE_27_33": Text("신청일 기준 만 27~33세", "Age 27 to 33 on the application date"),
    "AGE_34_40": Text("신청일 기준 만 34~40세", "Age 34 to 40 on the application date"),
    "AGE_41_PLUS": Text("신청일 기준 만 41세 이상", "Age 41 or older on the application date"),
    "REC_CENTRAL": Text(
        "중앙부처 추천(고용부·산업부·농식품부·해수부·국토부·중기부). "
        "광역지자체 추천과 중복 시 하나만",
        "Recommendation from a central government ministry (Employment and Labor; Trade, Industry"
        " and Energy; Agriculture, Food and Rural Affairs; Oceans and Fisheries; Land,"
        " Infrastructure and Transport; or SMEs and Startups). If you also have a provincial"
        " government recommendation, only one counts",
    ),
    "REC_REGIONAL": Text(
        "광역지자체 추천(17개 시·도). 중앙부처 추천과 중복 시 하나만",
        "Recommendation from a provincial government (one of the 17 metropolitan cities and"
        " provinces). If you also have a central government ministry recommendation,"
        " only one counts",
    ),
    "REC_EMPLOYER": Text(
        "고용기업 추천(필수요건이면서 가점)",
        "Employer recommendation (a requirement that also gives bonus points)",
    ),
    "TENURE_3Y": Text(
        "현 근무처 3년 이상 계속 근무. "
        "같은 사업장 재고용 기간과 귀책 없는 이직 직전 근무처 기간 합산",
        "3 years or more of continuous work at your current workplace. Re-employment at the same"
        " workplace counts, and so does the previous workplace if you left it through no fault"
        " of your own",
    ),
    "REGION_3Y": Text(
        "최근 10년 내 인구감소지역 또는 읍·면지역 3년 이상 근무(서로 합산, 겹친 기간은 한 번)",
        "3 years or more of work in a depopulation area or a town or township (eup or myeon)"
        " in the last 10 years (periods are added together, and overlapping periods count once)",
    ),
    "QUALIFICATION": Text(
        "현재 근무 분야 관련 국내 자격증(기사·산업기사·기능사) 또는 국내 전문학사 이상 학위"
        "(독학사·학점은행제 인정)",
        "A Korean national certificate related to your current field of work (Engineer,"
        " Industrial Engineer or Craftsman) or a Korean associate degree or higher (degrees"
        " through self-study or the Academic Credit Bank System count)",
    ),
    "DRIVER_LICENSE": Text(
        "국내 경찰청 발급 운전면허 제2종 보통 이상",
        "A Korean driver's license issued by the Korean National Police Agency,"
        " Class 2 Ordinary or higher",
    ),
    "REQ_STAY_PERIOD": Text(
        "최근 10년간 E-9·E-10·H-2로 합계 4년 이상 체류. "
        "특례: 비수도권 3년 이상 체류 + 광역지자체 추천"
        "(Q8, 검토 필요)",
        "A total stay of 4 years or more on E-9, E-10 or H-2 in the last 10 years. Special case:"
        " 3 years or more of stay outside the capital area with a provincial government"
        " recommendation",
        "최근 10년간 E-9·E-10·H-2로 합계 4년 이상 체류. "
        "특례: 비수도권 3년 이상 체류 + 광역지자체 추천",
    ),
    "REQ_CURRENT_EMPLOYMENT": Text(
        "현재 근무처에서 외국인등록을 하고 합법적으로 정상 근무 중",
        "Working legally at your current workplace with foreign resident registration",
    ),
    "REQ_CONTRACT": Text(
        "현재 근무처와 향후 2년 이상 E-7-4 고용계약, "
        "연봉 2,600만원 이상(기본급·수당 포함 총 지급액)",
        "An E-7-4 employment contract with your current workplace for the next 2 years or more,"
        " with an annual salary of KRW 26,000,000 or more (total pay including base pay and"
        " allowances)",
    ),
    "REQ_CONTRACT_AGRI_FISHERY": Text(
        "농·축산업, 어업·내항상선 종사자는 연봉 요건 2,500만원 이상",
        "For workers in farming, livestock, fishing or coastal shipping, the salary requirement"
        " is KRW 25,000,000 or more",
    ),
    "REQ_EMPLOYER_REC": Text(
        "현재 1년 이상 근무 중인 기업의 추천(귀책 없는 이직이면 직전 근무처와 합산)",
        "A recommendation from the company where you have worked for 1 year or more (the"
        " previous workplace counts if you left it through no fault of your own)",
    ),
    "REQ_MIN_INCOME": Text(
        "최근 2년 연간 평균소득 2,500만원 이상(평균소득 점수 50점 이상). "
        "재입국 공백 시 2021~2025년 중 높은 2개년",
        "Average annual income over the last 2 years of KRW 25,000,000 or more (an income score"
        " of 50 points or more). After a gap from re-entry, the 2 highest years from 2021 to"
        " 2025 are used",
    ),
    "REQ_MIN_INCOME_AGRI_FISHERY": Text(
        "농·축산업, 어업·내항상선 종사자는 평균소득 2,400만원 이상",
        "For workers in farming, livestock, fishing or coastal shipping, average income of"
        " KRW 24,000,000 or more",
    ),
    "REQ_MIN_KOREAN": Text(
        "한국어 점수 50점 이상. 2026-12-31까지 한국어 외 총점 150점 이상이면 최초 연장 때까지 유예",
        "A Korean score of 50 points or more. Until 2026-12-31, this is deferred until the first"
        " extension if the total without Korean is 150 points or more",
    ),
    "REQ_TOTAL_SCORE": Text(
        "300점 만점(가점 제외)에서 가점 포함 총점 200점 이상",
        "A total of 200 points or more including bonus points, out of 300 points"
        " (not counting bonus points)",
    ),
}

# --- 서류 이름(improvements[].documents). key는 서류 종류
DOCUMENTS: dict[str, Text] = {
    "INCOME_CERTIFICATE": Text(
        "세무서 발행 소득금액증명원(최근 2년)",
        "Income certificate issued by the tax office (last 2 years)",
    ),
    "KOREAN_RESULT": Text(
        "TOPIK 성적표 또는 사회통합프로그램 이수증·사전평가 성적표",
        "TOPIK score report, or KIIP completion certificate or pre-test score report",
    ),
    "ALIEN_REGISTRATION_CARD": Text("외국인등록증", "Alien registration card"),
    "PASSPORT_COPY": Text(
        "여권 사본(인적사항면)", "Copy of your passport (personal information page)"
    ),
    "BUSINESS_REGISTRATION_COPY": Text(
        "현재 근무처 사업자등록증 사본",
        "Copy of your current workplace's business registration certificate",
    ),
    "MINISTRY_RECOMMENDATION": Text(
        "중앙부처 추천서", "Recommendation letter from a central government ministry"
    ),
    "PROVINCE_RECOMMENDATION": Text(
        "광역지자체 추천서", "Recommendation letter from a provincial government"
    ),
    "EMPLOYER_RECOMMENDATION": Text(
        "고용기업 추천서(붙임5)",
        "Employer recommendation letter (official form)",
        "고용기업 추천서(공식 양식)",
    ),
    "RECOMMENDER_ID_COPY": Text("추천자 신분증 사본", "Copy of the recommender's ID"),
    "WORK_HISTORY_CERTIFICATE": Text(
        "외국인근로자 경력증명서(근무 이력)", "Foreign worker employment history certificate"
    ),
    "QUALIFICATION_COPY": Text(
        "국내 자격증 사본 또는 국내 학위증 사본",
        "Copy of your Korean national certificate or Korean degree certificate",
    ),
    "DRIVER_LICENSE_COPY": Text("국내 운전면허증 사본", "Copy of your Korean driver's license"),
    "EMPLOYMENT_CONTRACT": Text(
        "E-7-4 고용계약서(붙임9 견본)",
        "E-7-4 employment contract (official sample form)",
        "E-7-4 고용계약서(공식 견본 양식)",
    ),
}

# --- 규칙 조건 단어(자격증 등급·학위 과정). key는 규칙 JSON에 적힌 한국어 단어
CONDITION_TERMS: dict[str, str] = {
    "기사": "Engineer",
    "산업기사": "Industrial Engineer",
    "기능사": "Craftsman",
    "전문학사": "associate degree",
    "학사": "bachelor's degree",
    "석사": "master's degree",
    "박사": "doctorate",
}

# --- 검토 메모(reviewNotes[].note, 사람 확인용). key는 LLM 안내 코드(reviewNotices[].code)
REVIEW_NOTES: dict[str, Text] = {
    "NO_APPROVED_RULES": Text(
        "{date}에 적용할 승인된 규칙 버전이 없음(Q9)",
        "No approved scoring rules apply to {date}, so there is no score",
        "{date}에 적용할 승인된 점수 규칙이 없어 점수가 없음",
    ),
    "AGE_OUTSIDE_SCORE_TABLE": Text(
        "점수표에 없는 나이(19세 미만)",
        "The age is not in the score table (under 19), so this item needs a manual check",
        "점수표에 없는 나이(19세 미만)라 이 항목은 따로 확인해야 함",
    ),
    "INCOME_FROM_EARLIER_YEARS": Text(
        "최근 {years}년 소득이 없어 {start}~{end}년 중 높은 해로 계산함. "
        "재입국 공백 등 대체 사유 확인 필요",
        "Income for the last {years} years is missing, so the highest years from {start} to"
        " {end} were used. The reason, such as a gap after re-entry, needs a manual check",
        "최근 {years}년 소득이 없어 {start}~{end}년 중 높은 해로 계산함. "
        "재입국 공백 같은 사유는 따로 확인해야 함",
    ),
    "KOREAN_LEVEL_UNREADABLE": Text(
        "한국어 증빙의 급수·단계를 읽을 수 없음",
        "The level or stage on the Korean language document could not be read,"
        " so it needs a manual check",
        "한국어 증빙의 급수·단계를 읽을 수 없어 따로 확인해야 함",
    ),
    "CERTIFICATE_GRADE_NOT_LISTED": Text(
        "기술사·기능장은 선발계획 인정 목록(기사·산업기사·기능사)에 없어 확인 필요",
        "Professional Engineer and Master Craftsman certificates are not on the recognized list"
        " (Engineer, Industrial Engineer, Craftsman), so this needs a manual check",
        "기술사·기능장은 선발계획 인정 목록(기사·산업기사·기능사)에 없어 따로 확인해야 함",
    ),
    "STAY_SPECIAL_RULE_CHECK": Text(
        "Q8: 비수도권 3년 체류 특례(광역지자체 추천) 적용 여부 확인 필요",
        "Whether the special stay rule (3 years outside the capital area with a provincial"
        " government recommendation) applies needs a manual check",
        "비수도권 3년 체류 특례(광역지자체 추천)가 적용되는지 따로 확인해야 함",
    ),
    "REQUIRED_TOTAL_UNKNOWN": Text(
        "규칙 버전에 기준 총점이 없음(공식 확인 전)",
        "The required total score for these rules is not confirmed yet",
        "이 규칙 버전의 기준 총점이 아직 확정되지 않음",
    ),
}
# 위 목록에 없는 메모(규칙 데이터의 reviewNote 등)를 LLM에 알릴 때 쓰는 코드와 문구.
MANUAL_CHECK = "MANUAL_CHECK"
MANUAL_CHECK_TEXT = "This item needs a manual check"
MANUAL_CHECK_TEXT_KO = "이 항목은 따로 확인해야 함"

# --- 판정 상태(LLM 결과 statusText). key는 EligibilityStatus 값.
# 사전진단 결과이며 허가가 아니다.
# 상태 코드(ELIGIBLE 등)는 LLM에 주지 않는다.
# 카드(E74_SCORE_CARD.status)는 코드 그대로다(앱이 번역).
STATUSES: dict[str, Text] = {
    "ELIGIBLE": Text(
        "사전 점검 기준으로 신청 요건을 모두 충족함",
        "Meets all application requirements in this pre-check",
    ),
    "INELIGIBLE": Text(
        "지금은 신청 요건을 충족하지 못함",
        "Does not meet the application requirements now",
    ),
    "NEEDS_EVIDENCE": Text(
        "증빙 서류나 추가 정보가 있어야 신청 가능 여부를 판정할 수 있음",
        "Needs documents or more information before eligibility can be decided",
    ),
    "NEEDS_REVIEW": Text(
        "따로 확인해야 신청 가능 여부를 판정할 수 있음",
        "Needs a manual check before eligibility can be decided",
    ),
}


# --- 문구 만들기(엔진) -------------------------------------------------------------------------


def placeholders(text: str) -> set[str]:
    """문구에 든 {이름} 자리."""
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def or_list(words: Sequence[str]) -> str:
    """영어 나열: A / A or B / A, B or C."""
    if len(words) <= 1:
        return "".join(words)
    return f"{', '.join(words[:-1])} or {words[-1]}"


def improvement_text(key: str, **params: object) -> Text:
    """IMPROVEMENTS[key]에 값을 넣는다. 값이 Text면 한국어·영어에 언어별 문구를 넣는다."""
    text = IMPROVEMENTS[key]
    ko = text.ko.format(**{n: v.ko if isinstance(v, Text) else v for n, v in params.items()})
    if text.en is None or any(isinstance(v, Text) and v.en is None for v in params.values()):
        return Text(ko, None)
    en = text.en.format(**{n: v.en if isinstance(v, Text) else v for n, v in params.items()})
    return Text(ko, en)


def rule_text(rule_code: str, description: str) -> Text:
    """규칙 설명의 영어. 사전에 없거나 규칙 JSON 설명과 다르면 영어가 없다."""
    known = RULES.get(rule_code)
    return Text(description, known.en if known is not None and known.ko == description else None)


def visa_types_text(visa_types: Sequence[str]) -> Text:
    return Text("·".join(visa_types), or_list(visa_types))


def terms_text(words: Sequence[str]) -> Text:
    """규칙 조건 단어 나열(예: 기사·산업기사·기능사)."""
    english = [CONDITION_TERMS.get(w) for w in words]
    known = [w for w in english if w is not None]
    return Text("·".join(words), or_list(known) if len(known) == len(english) else None)


def term_text(word: str) -> Text:
    return Text(word, CONDITION_TERMS.get(word))


def review_text(key: str, **params: object) -> str:
    """REVIEW_NOTES[key]의 한국어 메모(엔진 출력 reviewNotes)."""
    return REVIEW_NOTES[key].ko.format(**params)


# --- LLM 도구 결과용 문구(사용자 언어) -------------------------------------------------------

KOREAN = "ko"


def is_korean(language: str) -> bool:
    """LLM 문구를 한국어로 줄 사용자 언어인지. ko만 한국어, 그 밖(en·vi·zh)은 영어."""
    return language == KOREAN


@cache
def _llm_korean_replacements() -> tuple[tuple[str, str], ...]:
    pairs = [(t.ko, t.llm_ko) for table in (RULES, DOCUMENTS) for t in table.values() if t.llm_ko]
    return tuple(sorted(pairs, key=lambda pair: -len(pair[0])))


def status_text(status: str, language: str) -> str:
    """판정 상태 코드 → 사용자 언어 문구(LLM 결과 statusText). ko면 한국어, 그 밖이면 영어."""
    text = STATUSES[status]
    return text.ko if is_korean(language) or text.en is None else text.en


def korean_for_llm(korean: str) -> str:
    """엔진 한국어 → LLM용 한국어. 규칙 설명·서류 이름에 든 내부 표기를 llm_ko 문구로 바꾼다.

    개선 방안 설명은 규칙 설명을 그대로 품으므로 같은 바꾸기로 충분하다. 저장 결과에도 그대로 쓴다.
    """
    for ko, llm in _llm_korean_replacements():
        korean = korean.replace(ko, llm)
    return korean


@cache
def _documents_by_ko() -> dict[str, str]:
    return {t.ko: t.en for t in DOCUMENTS.values() if t.en is not None}


def english_documents(documents: Sequence[str]) -> tuple[str, ...] | None:
    """서류 이름의 영어(같은 순서). 하나라도 사전에 없으면 None."""
    by_ko = _documents_by_ko()
    if any(d not in by_ko for d in documents):
        return None
    return tuple(by_ko[d] for d in documents)


@cache
def _rules_by_ko() -> dict[str, str]:
    return {t.ko: t.en for t in RULES.values() if t.en is not None}


def _terms_en(value: str) -> str | None:
    return terms_text(value.split("·")).en


# 한국어 문구에서 찾은 {이름} 자리 값을 영어 문구에 넣을 값으로 바꾼다. 없는 이름은 그대로 넣는다.
_EN_VALUE: dict[str, Callable[[str], str | None]] = {
    "rule": lambda v: _rules_by_ko().get(v),
    "visa_types": lambda v: or_list(v.split("·")),
    "grades": _terms_en,
    "degree": CONDITION_TERMS.get,
}


def _pattern(korean: str) -> re.Pattern[str]:
    parts = []
    for literal, name, _, _ in string.Formatter().parse(korean):
        parts.append(re.escape(literal))
        if name:
            parts.append(f"(?P<{name}>.+?)")
    return re.compile("".join(parts))


def _patterns(texts: dict[str, Text]) -> tuple[tuple[str, re.Pattern[str], Text], ...]:
    # {이름} 자리가 적은 문구부터 맞춘다. 자리가 많은 문구가 고정 문구를 먼저 잡지 않게.
    ordered = sorted(texts.items(), key=lambda kv: len(placeholders(kv[1].ko)))
    return tuple((key, _pattern(t.ko), t) for key, t in ordered)


@cache
def _improvement_patterns() -> tuple[tuple[str, re.Pattern[str], Text], ...]:
    return _patterns(IMPROVEMENTS)


@cache
def _review_patterns() -> tuple[tuple[str, re.Pattern[str], Text], ...]:
    return _patterns(REVIEW_NOTES)


def _english_values(match: re.Match[str]) -> dict[str, str] | None:
    values: dict[str, str] = {}
    for name, value in match.groupdict().items():
        convert = _EN_VALUE.get(name)
        english = convert(value) if convert is not None else value
        if english is None:
            return None
        values[name] = english
    return values


def _english(
    korean: str, patterns: tuple[tuple[str, re.Pattern[str], Text], ...]
) -> tuple[str, str] | None:
    for key, pattern, text in patterns:
        match = pattern.fullmatch(korean)
        if match is None or text.en is None:
            continue
        values = _english_values(match)
        if values is None:
            continue
        en = text.en.format(**values)
        if not HANGUL.search(en):
            return key, en
    return None


def english_improvement(korean: str) -> str | None:
    """개선 방안 한국어 설명 → 같은 문구의 영어. descriptionEn이 없던 저장 결과(#108 전)에 쓴다."""
    found = _english(korean, _improvement_patterns())
    return None if found is None else found[1]


def review_notice(note: str, language: str = "en") -> tuple[str, str]:
    """검토 메모 → (LLM 안내 코드, 사용자 언어 안내). 목록에 없는 메모는 MANUAL_CHECK.

    ko면 llm_ko(내부 표기 없는 한국어), 그 밖이면 영어 안내다. 메모 원문은 어느 쪽에도 주지 않는다.
    """
    if not is_korean(language):
        return _english(note, _review_patterns()) or (MANUAL_CHECK, MANUAL_CHECK_TEXT)
    for key, pattern, text in _review_patterns():
        match = pattern.fullmatch(note)
        if match is not None and text.llm_ko is not None:
            return key, text.llm_ko.format(**match.groupdict())
    return MANUAL_CHECK, MANUAL_CHECK_TEXT_KO

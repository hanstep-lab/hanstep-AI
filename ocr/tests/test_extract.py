"""extract.py 회귀 테스트. OCR 없이 글자 목록만 넣어서 필드 추출 규칙을 확인한다."""

import pytest

from pipeline.extract import extract_fields


def fields(lines):
    return {f["name"]: f for f in extract_fields(lines)}


# 견본 앞면 OCR 결과 22줄 (11/15). 규칙 테스트라 신뢰도는 0.99로 통일
SAMPLE = [(t, 0.99) for t in [
    "1", "KOR", "외국인등록증", "RESIDENCECARD", "외국인등록번호", "123456-1234567",
    "Registration No", "987654", "성명", "HONG SAMPLE", "Narne", "(홍샘플)",
    "국가/지역", "REPUBLIC OF UTOPIA", "Country/Region", "체류자격", "기업투자(D-8)",
    "Status", "발급일자 Issue Date 2023.04.01.", "3456", "서울출입국·외국인청장",
    "CHIEF,SEOUL IMMIGRATION OFFICE",
]]


def test_견본_정상():
    f = fields(SAMPLE)
    assert f["NAME_EN"]["value"] == "HONG SAMPLE"
    assert f["VISA_TYPE"]["value"] == "D-8"
    assert f["REGISTRATION_NO"]["value"] == "123456-1******"
    assert f["NATIONALITY"]["value"] is None  # 사전에 없는 가상 국가


def test_이름줄_누락시_다음필드_값을_이름으로_쓰지_않음():
    f = fields([("성명 Name", 0.99), ("국가 Country", 0.99), ("PHILIPPINES", 0.99)])
    assert f["NAME_EN"]["value"] is None
    assert f["NATIONALITY"]["value"] == "PHL"


def test_라벨과_이름이_한줄일때():
    f = fields([("성명 Name SAMPLE PERSON", 0.99), ("국가 Country", 0.99), ("PHILIPPINES", 0.99)])
    assert f["NAME_EN"]["value"] == "SAMPLE PERSON"


@pytest.mark.parametrize("text, expected", [
    ("E-9", "E-9"),
    ("E-7-4", "E-7-4"),
    ("F-1-13", "F-1-13"),
    ("F-2-99", "F-2-99"),
    ("기업투자(D-8)", "D-8"),
])
def test_체류자격(text, expected):
    assert fields([(text, 0.99)])["VISA_TYPE"]["value"] == expected


def test_지원하지_않는_체류자격은_일부만_잘라_쓰지_않음():
    assert fields([("F-1-134", 0.99)])["VISA_TYPE"]["value"] is None


def test_국가_영문라벨이_값보다_먼저_나와도_읽음():
    f = fields([("국가/지역", 0.99), ("Country/Region", 0.99), ("PHILIPPINES", 0.99)])
    assert f["NATIONALITY"]["value"] == "PHL"


# 11/23 실제 사진 측정에서 국적이 19장 모두 None으로 나온 버그.
# 카드에는 정식 국가명(REPUBLIC OF THE ...)이 찍히는데 사전은 짧은 이름만 정확히 일치로 찾았다
@pytest.mark.parametrize("country, code", [
    ("REPUBLIC OF THE PHILIPPINES", "PHL"),
    ("SOCIALIST REPUBLIC OF VIET NAM", "VNM"),
    ("VIETNAM", "VNM"),             # 기존 정확 일치는 그대로
    ("REPUBLIC OF UTOPIA", None),   # 사전에 없는 나라는 계속 빈칸
])
def test_정식_국가명도_코드로_변환(country, code):
    lines = [(country if t == "REPUBLIC OF UTOPIA" else t, s) for t, s in SAMPLE]
    assert fields(lines)["NATIONALITY"]["value"] == code
    
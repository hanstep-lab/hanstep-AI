"""국적 변환(pipeline/country.py + extract._nationality) 테스트. 이미지 없이 글자 목록만 넣는다."""

import pytest
from country_cases import CASES, card_lines

from pipeline.country import find_country, normalize
from pipeline.extract import extract_fields


def nationality(lines):
    return {f["name"]: f for f in extract_fields(lines)}["NATIONALITY"]


# 측정 목록 77개: 성공률은 scripts/eval_country.py로 보고, 테스트는 "다른 나라 코드로 바꾸지 않음"만 강제한다
@pytest.mark.parametrize("kind, text, expected", CASES)
def test_측정목록_잘못된_코드로_바꾸지_않음(kind, text, expected):
    assert nationality(card_lines(text))["value"] in (None, expected)


@pytest.mark.parametrize("raw, clean, corrected", [
    ("REPUBLIC OF THE PHILIPPINES", "REPUBLIC OF THE PHILIPPINES", False),
    ("PEOPLE'S REPUBLIC OF CHINA", "PEOPLES REPUBLIC OF CHINA", False),
    ("TIMOR-LESTE", "TIMOR LESTE", False),
    ("MONGOLIA.", "MONGOLIA", False),
    ("PHlLIPPINES", "PHILIPPINES", True),   # 소문자 l → I
    ("CAMB0DIA", "CAMBODIA", True),         # 숫자 0 → O
    ("KYRGYZ5TAN", "KYRGYZSTAN", True),     # 숫자 5 → S
])
def test_글자_정리(raw, clean, corrected):
    assert normalize(raw) == (clean, corrected)


def test_카드_표기와_정확히_같으면_신뢰도_유지():
    assert nationality(card_lines("REPUBLIC OF THE PHILIPPINES")) == {
        "name": "NATIONALITY", "value": "PHL", "confidence": 0.99}


def test_라벨과_한줄이어도_정확히_같으면_신뢰도_유지():
    f = nationality([("국가/ 지역 REPUBLIC OF THE PHILIPPINES", 0.98), ("Country/Rogion", 0.98)])
    assert f == {"name": "NATIONALITY", "value": "PHL", "confidence": 0.98}


@pytest.mark.parametrize("text", ["PHlLIPPINES", "CAMB0DIA", "VIETNM", "BANGIADESH"])
def test_글자를_고치거나_비슷한_이름이면_값은_내고_신뢰도는_낮춤(text):
    f = nationality(card_lines(text))
    assert f["value"] is not None and f["confidence"] == 0.5


@pytest.mark.parametrize("text", [
    "REPUBLIC OF CHINA", "TAIWAN", "REPUBLIC 0F CHINA", "REPUBLlC OF CHINA",
    "REPUBLIK OF CHINA",  # CHINA만 단어로 남아도 대만일 수 있어 정하지 않는다
    "REPUBLIC OF CHNA",
])
def test_대만은_CHN으로_바꾸지_않음(text):
    assert nationality(card_lines(text))["value"] is None


@pytest.mark.parametrize("text", ["CHINA", "PEOPLE'S REPUBLIC OF CHINA"])
def test_중국은_CHN(text):
    assert nationality(card_lines(text))["value"] == "CHN"


# 공식 이름 전체로 비교하면 'REPUBLIC OF INDIA'가 'REPUBLIC OF INDONESIA'와 비슷하게 나온다
@pytest.mark.parametrize("text", ["REPUBLIC OF INDIA", "INDIA", "MALAYSIA", "TURKMENISTAN", "REPUBLIC OF KOREA"])
def test_사전에_없는_나라는_비슷해도_null(text):
    assert nationality(card_lines(text))["value"] is None


def test_두줄로_나뉜_국가명을_이어서_읽음():
    f = nationality(card_lines("LAO PEOPLE'S DEMOCRATIC/REPUBLIC"))
    assert f["value"] == "LAO"


def test_라벨과_국가명_사이_잡음줄은_건너뜀():
    # 04_dark_b: 라벨 다음에 'I'(0.17)가 끼어 읽혔다
    f = nationality([("국가/지역", 1.00), ("I", 0.17), ("REPUBLIC OF THE PHILIPPINES", 0.98)])
    assert f == {"name": "NATIONALITY", "value": "PHL", "confidence": 0.98}


def test_두줄을_합치면_낮은_신뢰도를_씀():
    m = find_country([("FEDERAL DEMOCRATIC REPUBLIC", 0.95), ("OF NEPAL", 0.70)])
    assert (m.code, m.sure, m.score) == ("NPL", True, 0.70)


# 11/24 실제 사진 04_dark_a: 어두운 사진에서 PHILIPPINES가 PHILPPIHE로 읽혀 null이 됐다
def test_실제_사진_오타는_비슷한_이름으로_찾고_사용자_확인으로():
    f = nationality([("국가 /지역", 0.88), ("REPUBLIC OF THE PHILPPIHE", 0.93), ("Country/Rooon", 0.89)])
    assert f == {"name": "NATIONALITY", "value": "PHL", "confidence": 0.5}


def test_체류자격_라벨_뒤의_줄은_국가명으로_쓰지_않음():
    f = nationality([("국가/지역", 0.99), ("체류자격", 0.99), ("PHILIPPINES", 0.99)])
    assert f["value"] is None

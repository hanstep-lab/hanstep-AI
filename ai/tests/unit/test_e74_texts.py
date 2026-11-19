"""E-7-4 개선 방안·규칙 설명·서류·검토 메모 문구(#108): 한곳에 모은 한국어·영어 문구와 엔진 출력."""

from __future__ import annotations

import ast
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.visa.e74.engine import assess_e74
from app.visa.e74.rules import RuleCategory, load_rule_sets
from app.visa.e74.texts import (
    CONDITION_TERMS,
    DOCUMENTS,
    IMPROVEMENTS,
    MANUAL_CHECK,
    REVIEW_NOTES,
    RULES,
    Text,
    english_documents,
    english_improvement,
    improvement_text,
    or_list,
    placeholders,
    review_notice,
    review_text,
    rule_text,
    terms_text,
    visa_types_text,
)
from tests.unit._e74_builders import profile
from tests.unit._e74_cases import load_profiles

pytestmark = pytest.mark.unit

E74_DIR = Path(__file__).resolve().parents[2] / "app" / "visa" / "e74"
HANGUL = re.compile(r"[가-힣]")
RULE_SETS = load_rule_sets()
ALL_RULES = [rule for rule_set in RULE_SETS for rule in rule_set.rules]
# 개선 방안 문구에 규칙 설명이 들어갈 수 있는 범주(점수·가점 증빙, 요건 증빙, 추천 가점)
QUOTED_CATEGORIES = (
    RuleCategory.BASE_INCOME,
    RuleCategory.BASE_KOREAN,
    RuleCategory.BASE_AGE,
    RuleCategory.BONUS,
    RuleCategory.REQUIREMENT,
)
DATES = (date(2026, 10, 25), date(2026, 11, 19), date(2026, 12, 5))

# 문구의 {이름} 자리에 넣어 볼 값. Text 값은 한국어·영어가 다른 자리다.
SAMPLE_PARAMS: dict[str, object] = {
    "years": 4,
    "date": "2026-12-05",
    "visa_types": visa_types_text(("E-9", "E-10", "H-2")),
    "required": "26,000,000",
    "salary": "24,000,000",
    "shortfall": "2,000,000",
    "required_end": "2028-11-30",
    "contract_end": "2027-01-31",
    "months": 12,
    "average": "22,500,000",
    "topik": 2,
    "kiip": 2,
    "pre_test": 41,
    "stage": 3,
    "until": "2026-12-31",
    "min_total": 150,
    "needed": "35,000,000",
    "gain": 15,
    "rule": rule_text("REC_EMPLOYER", RULES["REC_EMPLOYER"].ko),
    "points": 50,
    "grades": terms_text(("기사", "산업기사", "기능사")),
    "degree": Text("전문학사", CONDITION_TERMS["전문학사"]),
}
REVIEW_PARAMS: dict[str, object] = {"date": "2027-03-01", "years": 2, "start": 2021, "end": 2025}


def _params(text: Text, sample: dict[str, object]) -> dict[str, object]:
    return {name: sample[name] for name in placeholders(text.ko)}


# --- 사전: 모든 key에 한국어·영어 ----------------------------------------------------------


@pytest.mark.parametrize(
    ("table", "key"),
    [
        (name, key)
        for name, texts in (
            ("IMPROVEMENTS", IMPROVEMENTS),
            ("RULES", RULES),
            ("DOCUMENTS", DOCUMENTS),
            ("REVIEW_NOTES", REVIEW_NOTES),
        )
        for key in texts
    ],
)
def test_every_text_has_korean_and_english(table: str, key: str) -> None:
    text = {
        "IMPROVEMENTS": IMPROVEMENTS,
        "RULES": RULES,
        "DOCUMENTS": DOCUMENTS,
        "REVIEW_NOTES": REVIEW_NOTES,
    }[table][key]
    assert text.ko.strip() and HANGUL.search(text.ko)
    assert text.en is not None and text.en.strip()
    assert not HANGUL.search(text.en), "영어 문구에 한글이 있다"
    assert placeholders(text.ko) == placeholders(text.en)
    # 내부 메모 번호(Q8 등)는 영어로 옮기지 않는다
    assert not re.search(r"\bQ\d+\b", text.en)


@pytest.mark.parametrize("word", sorted(CONDITION_TERMS))
def test_every_condition_term_has_english(word: str) -> None:
    assert HANGUL.search(word)
    assert CONDITION_TERMS[word].strip() and not HANGUL.search(CONDITION_TERMS[word])


def test_rule_texts_match_the_rule_json_and_cover_every_quoted_rule() -> None:
    quoted = {r.rule_code for r in ALL_RULES if r.category in QUOTED_CATEGORIES}
    assert sorted(quoted - set(RULES)) == [], "개선 방안에 나올 수 있는데 영어가 없는 규칙"
    assert sorted(set(RULES) - quoted) == [], "규칙 JSON에 없는 규칙"
    for rule in ALL_RULES:
        if rule.rule_code in RULES:
            assert RULES[rule.rule_code].ko == rule.description, rule.rule_code


def test_document_texts_cover_every_document_in_the_rule_json() -> None:
    documents = {d for rule in ALL_RULES for d in rule.documents}
    known = {t.ko for t in DOCUMENTS.values()}
    assert sorted(documents - known) == [], "영어가 없는 서류"
    assert sorted(known - documents) == [], "규칙 JSON에 없는 서류"


def test_condition_terms_cover_every_certificate_grade_and_degree_level() -> None:
    words = {
        word
        for c in (rule.condition for rule in ALL_RULES)
        for word in (*getattr(c, "certificate_grades", ()), *getattr(c, "degree_levels", ()))
    }
    assert words and sorted(words - set(CONDITION_TERMS)) == []


# --- 문구 만들기와 영어 찾기 -----------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(IMPROVEMENTS))
def test_english_improvement_finds_the_same_text_from_korean(key: str) -> None:
    built = improvement_text(key, **_params(IMPROVEMENTS[key], SAMPLE_PARAMS))
    assert built.en is not None and not HANGUL.search(built.en)
    assert english_improvement(built.ko) == built.en


@pytest.mark.parametrize("key", sorted(REVIEW_NOTES))
def test_review_notice_finds_the_code_and_english_from_the_note(key: str) -> None:
    params = _params(REVIEW_NOTES[key], REVIEW_PARAMS)
    english = REVIEW_NOTES[key].en
    assert english is not None
    assert review_notice(review_text(key, **params)) == (key, english.format(**params))


def test_unknown_texts_get_no_korean() -> None:
    assert english_improvement("모르는 설명") is None
    assert english_improvement("모르는 규칙: 증빙 서류를 내면 +20점 확정") is None
    assert english_documents(["세무서 발행 소득금액증명원(최근 2년)", "모르는 서류"]) is None
    code, text = review_notice("확인 필요(테스트)")
    assert code == MANUAL_CHECK and not HANGUL.search(text)


def test_text_without_english_keeps_korean_only() -> None:
    """규칙 JSON 설명이 사전과 다르면(사전 갱신 누락) 영어를 만들지 않는다. 한국어는 그대로다."""
    changed = rule_text("REC_EMPLOYER", "고용기업 추천(바뀐 설명)")
    built = improvement_text("E74_IMPROVE_SUBMIT_EVIDENCE", rule=changed, points=50)
    assert built == Text("고용기업 추천(바뀐 설명): 증빙 서류를 내면 +50점 확정", None)
    assert terms_text(("기사", "모르는 등급")).en is None


def test_or_list() -> None:
    assert [or_list(w) for w in ([], ["A"], ["A", "B"], ["A", "B", "C"])] == [
        "",
        "A",
        "A or B",
        "A, B or C",
    ]


# --- 엔진은 문구를 texts.py에서만 만든다 ------------------------------------------------------


def _engine_sources() -> dict[str, str]:
    return {
        p.name: p.read_text(encoding="utf-8")
        for p in sorted(E74_DIR.glob("*.py"))
        if p.name not in ("texts.py", "questions.py")
    }


def test_engine_has_no_korean_f_strings() -> None:
    """값이 들어가는 한국어 문구(개선 방안·검토 메모)는 texts.py 문구로만 만든다."""
    found = []
    for name, source in _engine_sources().items():
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.JoinedStr) and any(
                isinstance(v, ast.Constant) and isinstance(v.value, str) and HANGUL.search(v.value)
                for v in node.values
            ):
                found.append(f"{name}:{node.lineno}")
    assert found == []


def test_engine_review_notes_and_improvements_come_from_the_text_list() -> None:
    """검토 메모(review_note·note)와 개선 방안 설명에 문자열을 직접 쓰지 않는다."""
    found = []
    for name, source in _engine_sources().items():
        for node in ast.walk(ast.parse(source)):
            literal: ast.expr | None = None
            if isinstance(node, ast.keyword) and node.arg in ("review_note", "note"):
                literal = node.value
            elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "review_note" for t in node.targets
            ):
                literal = node.value
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in ("Improvement", "_fix", "make")
            ):
                found += [
                    f"{name}:{a.lineno}"
                    for a in [*node.args, *(k.value for k in node.keywords)]
                    if isinstance(a, ast.Constant | ast.JoinedStr) and HANGUL.search(ast.unparse(a))
                ]
            if isinstance(literal, ast.Constant | ast.JoinedStr) and HANGUL.search(
                ast.unparse(literal)
            ):
                found.append(f"{name}:{node.lineno}")
    assert found == []


# --- 샘플 프로필: 엔진 출력의 영어 --------------------------------------------------------------


ASSESSMENTS = [
    (f"{case['id']}@{as_of}", assess_e74(profile(case["profile"]), as_of, include_draft=True))
    for case in load_profiles()
    for as_of in DATES
]


@pytest.mark.parametrize(("label", "assessment"), ASSESSMENTS, ids=[a[0] for a in ASSESSMENTS])
def test_every_improvement_in_sample_profiles_has_english(label: str, assessment: Any) -> None:
    for imp in assessment.improvements:
        assert imp.description_en is not None, (label, imp.message_code)
        assert imp.documents_en is not None and len(imp.documents_en) == len(imp.documents)
        assert not HANGUL.search(" ".join((imp.description_en, *imp.documents_en)))
        # 영어가 없던 저장 결과도 한국어 문구로 같은 영어를 찾는다
        assert english_improvement(imp.description) == imp.description_en, imp.message_code
        assert english_documents(imp.documents) == imp.documents_en
    for note in assessment.review_notes:
        assert review_notice(note.note)[0] != MANUAL_CHECK, note.note

"""E-7-4 재질의 문구(#103): 한곳에 모은 한국어·영어 문구와 엔진 출력."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest

from app.visa.e74.engine import assess_e74
from app.visa.e74.questions import QUESTIONS, english_question, missing_input, placeholders
from app.visa.e74.schemas import LegalDeclarationIn
from tests.unit._e74_builders import AS_OF, profile
from tests.unit._e74_cases import load_profiles

pytestmark = pytest.mark.unit

E74_DIR = Path(__file__).resolve().parents[2] / "app" / "visa" / "e74"
HANGUL = re.compile(r"[가-힣]")
# 문구의 {이름} 자리에 넣어 볼 값
SAMPLE_PARAMS = {"year": 2024, "name": "용접기능사", "start": "2023-01-02", "end": "2024-05-31"}


def _engine_sources() -> dict[str, str]:
    return {
        p.name: p.read_text(encoding="utf-8")
        for p in sorted(E74_DIR.glob("*.py"))
        if p.name not in ("questions.py", "results.py")
    }


@pytest.mark.parametrize("key", sorted(QUESTIONS))
def test_every_question_has_korean_and_english(key: str) -> None:
    question = QUESTIONS[key]
    assert question.ko.strip()
    assert question.en.strip()
    assert HANGUL.search(question.ko)
    assert not HANGUL.search(question.en), "영어 문구에 한글이 있다"
    assert question.en.endswith("?")
    assert placeholders(question.ko) == placeholders(question.en)


def test_engine_builds_missing_inputs_only_through_the_question_list() -> None:
    """엔진 파일은 MissingInput을 직접 만들지 않는다(문구가 questions.py 밖에 생기지 않게)."""
    direct = []
    for name, source in _engine_sources().items():
        for node in ast.walk(ast.parse(source)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "MissingInput"
            ):
                direct.append(f"{name}:{node.lineno}")
    assert direct == []


def test_every_question_key_is_used_by_the_engine() -> None:
    sources = "\n".join(_engine_sources().values())
    declaration_keys = {
        f"legalDeclaration.{field.alias}" for field in LegalDeclarationIn.model_fields.values()
    }
    other_keys = set(QUESTIONS) - declaration_keys
    assert declaration_keys <= set(QUESTIONS)
    assert [key for key in sorted(other_keys) if f'"{key}"' not in sources] == []


@pytest.mark.parametrize("key", sorted(QUESTIONS))
def test_english_question_finds_the_same_question_from_korean(key: str) -> None:
    params = {name: SAMPLE_PARAMS[name] for name in placeholders(QUESTIONS[key].ko)}
    built = missing_input(key, field="x", **params)
    assert english_question(built.question) == built.question_en


def test_english_question_keeps_an_open_period_apart_from_a_closed_one() -> None:
    open_period = "2023-01-02~현재 근무지가 인구감소지역 또는 읍·면 지역인지"
    closed = "2023-01-02~2024-05-31 근무지가 인구감소지역 또는 읍·면 지역인지"
    assert english_question(open_period) == (
        "Is your workplace since 2023-01-02 in a depopulation area"
        " or a town or township (eup or myeon)?"
    )
    assert english_question(closed) == (
        "Was your workplace from 2023-01-02 to 2024-05-31 in a depopulation area"
        " or a town or township (eup or myeon)?"
    )


def test_english_question_is_none_for_unknown_text() -> None:
    assert english_question("모르는 질문") is None


@pytest.mark.parametrize("case", load_profiles(), ids=lambda c: c["id"])
def test_every_missing_input_in_sample_profiles_has_english(case: dict[str, Any]) -> None:
    result = assess_e74(profile(case["profile"]), AS_OF, include_draft=True)
    item_missing = [m for item in result.items for m in item.missing_inputs]
    for m in (*item_missing, *result.missing_inputs):
        assert m.question_en is not None, m.field
        assert english_question(m.question) == m.question_en, m.field

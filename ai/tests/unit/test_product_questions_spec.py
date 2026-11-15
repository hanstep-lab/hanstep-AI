"""금융상품 검색 평가 세트(specs/rag/product_questions_v1.json) 스키마 검증.

문항 내용은 사람이 검토해 고정한 값이다. 여기서는 형식만 확인하고 내용은 고치지 않는다.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.conftest import AI_ROOT

pytestmark = pytest.mark.unit

SPEC = AI_ROOT / "specs" / "rag" / "product_questions_v1.json"
QUESTION_COUNT = 30
STRING_FIELDS = ("id", "question", "goldDocument", "goldQuote")
FIELDS = {*STRING_FIELDS, "page", "namesProduct"}


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    data = json.loads(SPEC.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_top_level_has_version_and_questions_only(spec: dict[str, Any]) -> None:
    assert set(spec) == {"version", "questions"}
    assert isinstance(spec["version"], str) and spec["version"].strip()
    assert isinstance(spec["questions"], list)
    assert len(spec["questions"]) == QUESTION_COUNT


def test_every_question_has_exact_fields_and_types(spec: dict[str, Any]) -> None:
    for question in spec["questions"]:
        assert isinstance(question, dict)
        assert set(question) == FIELDS, question.get("id")
        for name in STRING_FIELDS:
            value = question[name]
            assert isinstance(value, str), (question["id"], name)
            assert value.strip() == value and value, (question["id"], name)
        # bool은 int의 하위 타입이라 따로 막는다.
        page = question["page"]
        assert isinstance(page, int) and not isinstance(page, bool), question["id"]
        assert page >= 1, question["id"]
        assert isinstance(question["namesProduct"], bool), question["id"]


def test_question_ids_are_unique(spec: dict[str, Any]) -> None:
    ids = [question["id"] for question in spec["questions"]]
    assert len(ids) == len(set(ids))


def test_both_names_product_groups_are_present(spec: dict[str, Any]) -> None:
    # namesProduct true/false를 나눠 집계하므로 두 집단이 모두 있어야 한다.
    assert {question["namesProduct"] for question in spec["questions"]} == {True, False}

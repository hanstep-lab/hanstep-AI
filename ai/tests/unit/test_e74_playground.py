from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from playground.e74_views import (
    ITEM_GROUPS,
    comparison,
    improvement_rows,
    item_rows,
    load_samples,
    missing_rows,
    parse_profile,
    plan_text,
    run,
)

pytestmark = pytest.mark.unit

AS_OF = date(2026, 10, 25)
APP = Path(__file__).resolve().parents[2] / "playground" / "e74_app.py"


def _sample(prefix: str) -> str:
    samples = load_samples()
    name = next(n for n in samples if n.startswith(prefix))
    return json.dumps(samples[name], ensure_ascii=False)


def test_samples_load_in_id_order() -> None:
    assert [name[:3] for name in load_samples()] == ["P00", "P01", "P02", "P03", "P04", "P05"]


def test_parse_reports_json_and_field_errors() -> None:
    profile, errors = parse_profile("{ not json")
    assert profile is None and errors[0].startswith("JSON 문법 오류: 1번째 줄")
    profile, errors = parse_profile('{"visaProfile": {"visaTypeCode": "E9"}, "extra": 1}')
    assert profile is None
    assert any(e.startswith("visaProfile.visaTypeCode") for e in errors)
    assert any(e.startswith("extra") for e in errors)


def test_tables_for_a_sample() -> None:
    profile, errors = parse_profile(_sample("P03"))
    assert profile is not None and errors == []
    a = run(profile, AS_OF, include_draft=True)
    rows = improvement_rows(a)
    assert rows[0]["순위"] == 1 and rows[0]["막힘"] == "예"
    assert {row["종류"] for row in rows} >= {"서류 제출", "구간 올리기", "가점 얻기"}
    assert [row["필드"] for row in missing_rows(a)] == [
        "legalDeclaration.taxStayRestrictionCount",
        "legalDeclaration.hasCurrentTaxArrears",
    ]
    counts = {name: len(item_rows(a, categories)) for name, categories in ITEM_GROUPS.items()}
    assert counts == {"기본항목": 3, "가점": 7, "감점": 3, "필수요건": 9, "제외": 6}
    assert plan_text(a).startswith("205점 부족")


def test_plan_text_when_already_eligible() -> None:
    profile, _ = parse_profile(_sample("P00"))
    assert profile is not None
    assert plan_text(run(profile, AS_OF, include_draft=True)) == "이미 기준 총점(200점)을 채움"


def test_comparison_only_when_input_changes() -> None:
    profile, _ = parse_profile(_sample("P00"))
    assert profile is not None
    first = run(profile, AS_OF, include_draft=True)
    assert comparison(None, first) is None
    assert comparison(first, run(profile, AS_OF, include_draft=True)) is None
    later = run(profile, date(2026, 10, 26), include_draft=True)
    diff = comparison(first, later)
    assert diff is not None and diff["status"] == ("ELIGIBLE", "ELIGIBLE")


def test_app_renders_samples_and_errors() -> None:
    """Streamlit이 있을 때만 실행: uv run --project ai --with streamlit pytest ..."""
    testing = pytest.importorskip("streamlit.testing.v1")
    at = testing.AppTest.from_file(str(APP), default_timeout=30).run()
    assert not at.exception
    assert any("결과:" in element.value for element in at.success)  # P00 → 신청 가능

    at.selectbox(key="sample").select(next(n for n in load_samples() if n.startswith("P02"))).run()
    assert not at.exception
    assert any("정보·증빙 필요" in element.value for element in at.warning)

    at.text_area(key="profile_text").input("{ broken").run()
    assert not at.exception
    assert any("계산할 수 없음" in element.value for element in at.error)


def test_chat_shows_tool_error_instead_of_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Streamlit이 있을 때만 실행: uv run --project ai --with streamlit pytest ..."""
    testing = pytest.importorskip("streamlit.testing.v1")
    from app.core.config import get_settings
    from playground import e74_chat

    def broken_turn(*_: object) -> e74_chat.ChatTurn:
        raise RuntimeError("홍길동 010-0000-0000")

    # e74_app은 실행할 때마다 e74_chat에서 run_turn을 가져오므로 모듈 속성을 바꾸면 된다.
    monkeypatch.setattr(e74_chat, "run_turn", broken_turn)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-0000")
    get_settings.cache_clear()
    try:
        at = testing.AppTest.from_file(str(APP), default_timeout=30).run()
        at.chat_input[0].set_value("제 점수는?").run()
    finally:
        get_settings.cache_clear()

    assert not at.exception
    texts = [element.value for element in at.markdown]
    assert any("도구 실행 오류(RuntimeError)" in text for text in texts)
    # 예외 메시지(입력 값이 섞일 수 있음)는 화면에 보이지 않는다.
    assert not any("홍길동" in text for text in texts)

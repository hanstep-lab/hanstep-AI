"""금융 정책 말뭉치(#88): HWPML, 예정 판 제외, 규정 조문, 청크 규칙, 검색 지표(모델 없음)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.finance_policy.chunks import fixed_chunks, unit_chunks
from app.finance_policy.corpus import (
    CorpusError,
    Unit,
    hwpml_lines,
    law_units,
    rule_units,
    section_units,
    split_law,
)

pytestmark = pytest.mark.unit
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _script() -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "finance_policy_search_eval", SCRIPTS / "finance_policy_search_eval.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EVAL = _script()


def _words(text: str) -> int:
    return len(text.split())


HWPML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE HWPML [
\t<!ENTITY nbsp\t"&#160;">
]>
<HWPML Version="2.1"><HEAD SecCnt="1"><DOCSUMMARY><TITLE>법</TITLE></DOCSUMMARY></HEAD>
<BODY><SECTION>
<P><TEXT><CHAR>제3조(정의) ① 뜻은&nbsp;다음과 같다.</CHAR></TEXT></P>
<P><TEXT><CHAR>  14. “거주자”란 국내에 주소를 둔 개인이다.</CHAR></TEXT></P>
<P><TEXT><CHAR>제3조(정의) ① 뜻은 다음과 같다(개정).</CHAR></TEXT></P>
<P><TEXT><CHAR>  [시행일: 2026. 12. 3.] 제3조</CHAR></TEXT></P>
<P><TEXT><CHAR>제4조(다른 조) 내용</CHAR></TEXT></P>
<P><TEXT><CHAR></CHAR></TEXT></P>
<P><TEXT><CHAR>부칙 &lt;제1호&gt;</CHAR></TEXT></P>
<P><TEXT><CHAR>제1조(시행일) 공포한 날부터 시행한다.</CHAR></TEXT></P>
</SECTION></BODY></HWPML>"""


def test_hwpml_lines_and_scheduled_versions() -> None:
    lines = hwpml_lines(HWPML)
    assert lines[0] == "제3조(정의) ① 뜻은 다음과 같다."  # nbsp → 공백, 빈 문단 없음
    articles = split_law(lines)
    assert [(a.article, a.scheduled_from, a.addenda) for a in articles] == [
        ("제3조", None, False),
        ("제3조", "2026.12.3", False),
        ("제4조", None, False),
        ("제1조", None, True),
    ]
    doc = {"key": "FETA", "articles": ["제3조", "제1조"], "effectiveFrom": "2026-01-02"}
    current, scheduled = law_units(doc, lines)
    # 현행만 색인, 예정 판은 따로(effective_from = 표시 날짜), 부칙 제1조는 본문이 아니다.
    assert [(u.unit_id, u.effective_from) for u in current] == [("FETA:제3조", "2026-01-02")]
    assert [(u.unit_id, u.effective_from) for u in scheduled] == [
        ("FETA:제3조:scheduled", "2026-12-03")
    ]


def test_law_item_only() -> None:
    lines = ["제2조(정의) 정의는 다음과 같다.", "  9. 아홉", "  10. “접근매체”라 함은 수단이다.",
             "    가. 카드", "  11. 열하나"]  # fmt: skip
    doc = {"key": "EFTA", "articles": ["제2조"], "items": {"제2조": "10"}, "effectiveFrom": "x"}
    (unit,), _ = law_units(doc, lines)
    assert unit.text.splitlines() == [
        "제2조(정의)",
        "10. “접근매체”라 함은 수단이다.",
        "    가. 카드",
    ]


def test_rule_units_strip_furniture_items_and_chapters() -> None:
    pages = {
        1: "\n".join(
            [
                "외국환거래규정",
                "제1-2조(용어의정의)정의는다음과같다.",
                '34."가"이라함은가.',
                '35."지정거래외국환은행"이라함은지정한은행을말한다.',
                '36."채권"이라함은채권.',
            ]
        ),
        2: "\n".join(
            [
                "법제처 -2/ 77- 국가법령정보센터",
                "제4-4조(비거주자의지급등) ① 연간미화5만불",
                "② 다음",
                "제5장 지급등의방법",
                "제5-1조(다음) 내용",
            ]
        ),
    }
    doc = {"key": "REG", "articles": ["제1-2조", "제4-4조"], "items": {"제1-2조": "35"},
           "notes": {"제4-4조": "확인"}, "effectiveFrom": "2026-11-02"}  # fmt: skip
    units = {u.ref: u for u in rule_units(doc, pages)}
    assert units["제1-2조"].text.splitlines() == [
        "제1-2조(용어의정의)",
        '35."지정거래외국환은행"이라함은지정한은행을말한다.',
    ]
    assert "법제처" not in units["제4-4조"].text and "제5장" not in units["제4-4조"].text
    assert units["제4-4조"].meta == {"note": "확인"}
    with pytest.raises(CorpusError, match="not found"):
        rule_units({**doc, "articles": ["제9-9조"]}, pages)


def test_section_units_only_listed_pages() -> None:
    pages = {1: "intro\n(1) First topic\ntext a", 2: "skip me", 3: "(2) Second\ntext b"}
    doc = {"key": "GUIDE", "pages": [1, 3], "headingPattern": r"^\s*\(\d+\)\s+\S"}
    units = section_units(doc, pages)
    assert [u.title for u in units] == ["intro", "(1) First topic", "(2) Second"]
    assert all("skip me" not in u.text for u in units)
    assert units[2].pages == (3,) and units[2].ref == "p3"


def _unit(doc: str, uid: str, text: str) -> Unit:
    return Unit(doc, f"{doc}:{uid}", uid, text, (1,), "2026-01-01")


def test_unit_chunk_rule_merges_short_and_splits_long() -> None:
    long_text = "\n".join(" ".join(["w"] * 100) for _ in range(10))  # 1000 단어, 10문단
    units = [
        _unit("G", "a", "short"),  # 30 미만 → 다음 단위에 붙음
        _unit("G", "b", " ".join(["x"] * 40)),
        _unit("G", "c", long_text),  # 900 초과 → 500 이하로
        _unit("H", "d", "tiny"),  # 문서 끝의 짧은 단위는 그대로
    ]
    chunks = unit_chunks(units, _words)
    assert chunks[0].unit_ids == ("G:a", "G:b") and chunks[0].text.startswith("short\n")
    long_parts = [c for c in chunks if c.unit_ids == ("G:c",)]
    assert len(long_parts) == 2 and all(_words(c.text) <= 500 for c in long_parts)
    assert chunks[-1].doc == "H" and chunks[-1].effective_from == "2026-01-01"
    fixed = fixed_chunks(units, _words)
    assert {c.doc for c in fixed} == {"G", "H"} and all(c.unit_ids == () for c in fixed)


def test_scoring_and_selection() -> None:
    from app.finance_policy.chunks import PolicyChunk

    c = [
        PolicyChunk("1", "REG", "가 나 다 연간 미화 5만불", ("REG:x",), 1, None),
        PolicyChunk("2", "REG", "건당미화5천불", ("REG:y",), 1, None),
        PolicyChunk("3", "GUIDE", "건당미화5천불", ("G:z",), 1, None),
    ]
    q = {
        "keyFacts": [
            {"doc": "REG", "quote": "연간미화5만불"},
            {"doc": "REG", "quote": "건당 미화 5천불"},
        ]
    }
    s = EVAL.score(q, [c[2], c[0]], c)
    assert s["hit"] and s["rank"] == 2 and s["rr"] == 0.5  # GUIDE 청크는 문서가 달라 근거가 아님
    assert s["factsCovered"] == [True, False] and not s["allFacts"]
    assert EVAL.score(q, [c[0], c[1]], c)["allFacts"] is True
    summary = {
        "1": {"n": 32, "recall": 30, "allFacts": 20, "mrr": 0.80},
        "2": {"n": 32, "recall": 29, "allFacts": 25, "mrr": 0.70},
        "3": {"n": 32, "recall": 27, "allFacts": 26, "mrr": 0.90},
    }
    sel = EVAL.select(summary)
    # Recall: 1·2 남음(1문항 차이는 동점) → 핵심 사실: 2(25)만 남음
    assert [s["kept"] for s in sel["steps"]] == [["1", "2"], ["2"], ["2"]]
    assert sel["chosen"] == "2" and sel["distinguished"] is True
    tie = {m: {"n": 32, "recall": 30, "allFacts": 20, "mrr": 0.5} for m in ("1", "2", "3")}
    assert EVAL.select(tie) == {**EVAL.select(tie), "chosen": "1", "distinguished": False}
    user = {"variant": "user", "text": "hey", "queryEn": "x", "queryKo": "가"}
    assert EVAL.query_text(user, "en") == "hey" and EVAL.query_text(user, "ko") == "가"

"""금융 정책 답변 위험 문장 검사 0원 기준선(#93): 저장된 evaluate_chat jsonl → md.

규칙은 ai/specs/rag/finance_policy_answer_guard_v1.md에 실행 전에 고정했다.
제4-4조 ④(건당 미화 5천불 이내 증빙 없이 '수령')를 '송금'으로 옮긴 문장을 찾는다.

- M(금액 USD 5,000) ∧ S(송금 방향) ∧ ¬R(수령 방향) = R1, R1 ∧ D(무증빙) = R2,
  M ∧ S ∧ R = 혼합(따로 셈). 한 답변에 걸린 문장이 하나라도 있으면 그 답변이 걸린 것이다.
- 확정 위험 8건(설계 문서 17-3절 7건 + 머지 재측정 A03#1)을 몇 개 잡는지,
  그 밖에서 몇 개 걸리는지(오탐) 센다.
- 외부 호출 없음. 신호(M·S·R)와 v3 규칙은 서버 모듈 app/finance_policy/answer_guard.py에서
  가져온다(표준 라이브러리만 쓰는 모듈, 규칙 중복 없음). v1·v2 전용 정규식만 여기에 둔다.

    uv run python scripts/eval/check_policy_answer_guard.py \\
        artifacts/chat/policy_after_v4_2.jsonl ... --output result.md [--notes notes.json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # ai/ (app 패키지)

from app.finance_policy.answer_guard import (  # noqa: E402
    MONEY,
    RECEIVE,
    SEND,
    classify_v3,
    has_d3,
)

_I = re.IGNORECASE
NO_DOCS = [
    re.compile(r"\bwithout\s+(?:supporting\s+)?(?:documents?|proof|evidence)\b", _I),
    re.compile(r"\bno\s+documents?\b", _I),
    re.compile(r"증빙\s?없이"),
]
# v2 D2(finance_policy_answer_guard_v2.md 2절): 서류 단어 ∧ 부정 표현이 같은 문장에.
DOC_WORDS = [
    re.compile(r"\bdocuments?\b", _I),
    re.compile(r"\bpaperwork\b", _I),
    re.compile(r"\bproof\b", _I),
    re.compile(r"\bevidence\b", _I),
    re.compile(r"증빙"),
    re.compile(r"서류"),
]
NEGATIONS = [
    re.compile(r"\bwithout\b", _I),
    re.compile(r"\bno\b", _I),
    re.compile(r"\bnot\b", _I),
    re.compile(r"\bdon['’]t\b", _I),
    re.compile(r"\bdoesn['’]t\b", _I),
    re.compile(r"\bdo\s+not\b", _I),
    re.compile(r"\bdoes\s+not\b", _I),
    re.compile(r"\bneedn['’]t\b", _I),
    re.compile(r"\bnot\s+required\b", _I),
    re.compile(r"\bno\s+need\b", _I),
    re.compile(r"없이"),
    re.compile(r"없어도"),
    re.compile(r"필요\s?없"),
]
# 오탐 분류 보조(표시만, 규칙 아님): 소액해외송금업 한도(제2-31조) 문장인지.
SMALL_SUM = re.compile(
    r"2-31|small[- ]?sum|money\s+transfer\s+app|remittance\s+(?:compan\w*|business\w*|app\w*|"
    r"provider\w*|operator\w*|firm\w*|service\w*)",
    _I,
)
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
_SPLIT = re.compile(r"(?<=[.!?])\s+")

# 확정 위험 오답(파일 이름, '문항#회차').
# 설계 문서 17-3절 7건 + reviews/finance_policy_merged_v4_2_review.md A03#1(태건 확정).
_AFTER_RISKY = ("A01#1", "A01v#2", "A01v#3", "A02#1", "A03#3", "A15v#1", "N04#3")
CONFIRMED_RISKY: frozenset[tuple[str, str]] = frozenset(
    {("policy_after_v4_2.jsonl", key) for key in _AFTER_RISKY}
    | {("policy_merged_labels_v4_2.jsonl", "A03#1")}
)
# 사람 판정 파일(표시용). 머지 재측정은 리뷰 문서의 answerable 표를 읽는다.
GRADE_FILES = {
    "policy_baseline_v4_1.jsonl": "policy_baseline_v4_1_grades_draft.json",
    "policy_after_v4_2.jsonl": "policy_after_v4_2_grades_draft.json",
    "policy_guard_v4_3a.jsonl": "policy_guard_v4_3a_grades_draft.json",
    "policy_guard_labels_v4_2.jsonl": "policy_guard_labels_v4_2_grades_draft.json",
}
MERGED_REVIEW = "specs/rag/reviews/finance_policy_merged_v4_2_review.md"
_REVIEW_ROW = re.compile(r"^\| (A\d+v?#\d) \| (?:original|user) \| (\S+) \|", re.M)
KO_GRADE = {"정답": "correct", "부분": "partial", "오답": "wrong", "기권": "abstain"}


def _any(patterns: Iterable[re.Pattern[str]], text: str) -> bool:
    return any(p.search(text) for p in patterns)


def sentences(answer: str) -> list[str]:
    out: list[str] = []
    for line in answer.splitlines():
        line = _BULLET.sub("", line).strip()
        if line:
            out.extend(s.strip() for s in _SPLIT.split(line) if s.strip())
    return out


@dataclass(frozen=True)
class Hit:
    sentence: str
    rule: str  # "R1" | "R2" | "mixed"


def classify(sentence: str) -> str | None:
    """한 문장의 규칙. 'R2'는 R1 조건도 만족한다(R2 ⊂ R1)."""
    if not (_any(MONEY, sentence) and _any(SEND, sentence)):
        return None
    if _any(RECEIVE, sentence):
        return "mixed"
    return "R2" if _any(NO_DOCS, sentence) else "R1"


def has_d2(sentence: str) -> bool:
    return _any(DOC_WORDS, sentence) and _any(NEGATIONS, sentence)


def classify_v2(sentence: str) -> str | None:
    """v2: M ∧ S ∧ ¬R ∧ D2 = 'R2v2'. M ∧ S ∧ R은 'mixed'(참고로만 센다)."""
    if not (_any(MONEY, sentence) and _any(SEND, sentence)):
        return None
    if _any(RECEIVE, sentence):
        return "mixed"
    return "R2v2" if has_d2(sentence) else None


CLASSIFIERS = {"v1": classify, "v2": classify_v2, "v3": classify_v3}
# 엄격 규칙(v2·v3) 결과 md의 규칙별 문구. v2 문구는 v2 결과 md와 같게 둔다.
STRICT = {
    "v2": {
        "hit": "R2v2",
        "doc": "finance_policy_answer_guard_v2.md",
        "formula": "R2v2 = M ∧ S ∧ ¬R ∧ D2(서류 단어 ∧ 부정 표현). "
        "혼합 = M ∧ S ∧ R(참고, 검출 아님).",
        "disclose": "**v2는 v1 누락 문장을 본 뒤 만든 규칙이라 아래는 표본 내 성적이다.**",
        "d": "D2",
        "has_d": has_d2,
    },
    "v3": {
        "hit": "R3",
        "doc": "finance_policy_answer_guard_v3.md",
        "formula": "R3 = M ∧ S ∧ ¬R ∧ D3(부정 표현·서류 단어 0~3단어 근접, 한국어 증빙·서류 없이). "
        "혼합 = M ∧ S ∧ R(참고, 검출 아님).",
        "disclose": "**v3는 v2 오탐(after A10#3)과 v1 누락 문장을 본 뒤 만든 규칙이라 "
        "아래는 표본 내 성적이다. 규칙 수정은 v3가 마지막이다.**",
        "d": "D3",
        "has_d": has_d3,
    },
}


def check_answer(answer: str, rule: str = "v1") -> list[Hit]:
    fn = CLASSIFIERS[rule]
    return [Hit(s, r) for s in sentences(answer) if (r := fn(s)) is not None]


@dataclass
class FileResult:
    name: str
    answers: int = 0
    r1: list[str] = field(default_factory=list)
    r2: list[str] = field(default_factory=list)
    mixed: list[str] = field(default_factory=list)
    hits: list[tuple[str, Hit]] = field(default_factory=list)


def answer_text(row: Mapping[str, Any]) -> str:
    parts = [row.get("answer") or ""]
    follow = row.get("followUp") or {}
    if follow.get("answer"):
        parts.append(follow["answer"])
    return "\n".join(p for p in parts if p)


def check_rows(name: str, rows: Iterable[Mapping[str, Any]], rule: str = "v1") -> FileResult:
    """v1: r1 = R1(R2 포함), r2 = R2. v2·v3: r1·r2 모두 R2v2·R3(같은 목록)."""
    res = FileResult(name)
    for row in rows:
        if row.get("type") != "call" or row.get("warmup"):
            continue
        res.answers += 1
        key = f"{row['questionId']}#{row['repeat']}"
        hits = check_answer(answer_text(row), rule)
        rules = {h.rule for h in hits}
        if rules & {"R1", "R2", "R2v2", "R3"}:
            res.r1.append(key)
        if rules & {"R2", "R2v2", "R3"}:
            res.r2.append(key)
        if "mixed" in rules:
            res.mixed.append(key)
        res.hits.extend((key, h) for h in hits)
    return res


def check_file(path: Path, rule: str = "v1") -> FileResult:
    lines = path.read_text(encoding="utf-8").splitlines()
    return check_rows(path.name, (json.loads(line) for line in lines if line.strip()), rule)


def load_grades(ai_root: Path, name: str) -> dict[str, str]:
    if name in GRADE_FILES:
        p = ai_root / "artifacts" / "chat" / GRADE_FILES[name]
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            return {k: v["grade"] for k, v in data.items()}
    if name == "policy_merged_labels_v4_2.jsonl":
        p = ai_root / MERGED_REVIEW
        if p.exists():
            rows = _REVIEW_ROW.findall(p.read_text(encoding="utf-8"))
            return {k: KO_GRADE.get(g, g) for k, g in rows}
    return {}


def _cell(s: str) -> str:
    return s.replace("|", "/").replace("\n", " ")


def _keys(pairs: Iterable[tuple[str, str]]) -> str:
    return ", ".join(f"{n} {k}" for n, k in sorted(pairs)) or "없음"


def render(
    results: Sequence[FileResult],
    grades: Mapping[str, Mapping[str, str]],
    excluded: Sequence[str],
    notes: Mapping[str, str],
) -> str:
    total = len(CONFIRMED_RISKY)
    o: list[str] = []
    a = o.append
    a("# 금융 정책 답변 위험 문장 검사 0원 기준선 (#93, 규칙 v1)")
    a("")
    a("규칙: `ai/specs/rag/finance_policy_answer_guard_v1.md`(실행 전 커밋).")
    a("저장된 답변만 읽었다(외부 호출 없음, 서버 코드 변경 없음).")
    a("R1 = M ∧ S ∧ ¬R, R2 = R1 ∧ D, 혼합 = M ∧ S ∧ R. 오탐 = 확정 위험 8건 밖에서 걸린 답변.")
    a("")
    a("## 1. 파일별 결과")
    a("")
    a("| 파일 | 답변 수 | R1 걸림 | R2 걸림 | 혼합 | 확정 위험 탐지 R1 / R2 (/8) | 오탐 R1 / R2 |")
    a("|---|---|---|---|---|---|---|")
    det1 = det2 = fp1 = fp2 = 0
    for r in results:
        c1 = sum((r.name, k) in CONFIRMED_RISKY for k in r.r1)
        c2 = sum((r.name, k) in CONFIRMED_RISKY for k in r.r2)
        f1, f2 = len(r.r1) - c1, len(r.r2) - c2
        det1, det2, fp1, fp2 = det1 + c1, det2 + c2, fp1 + f1, fp2 + f2
        a(
            f"| {r.name} | {r.answers} | {len(r.r1)} | {len(r.r2)} | {len(r.mixed)} "
            f"| {c1} / {c2} | {f1} / {f2} |"
        )
    n_ans = sum(r.answers for r in results)
    n_r1 = sum(len(r.r1) for r in results)
    n_r2 = sum(len(r.r2) for r in results)
    n_mx = sum(len(r.mixed) for r in results)
    a(
        f"| **합계** | {n_ans} | {n_r1} | {n_r2} | {n_mx} "
        f"| **{det1} / {det2}** (/{total}) | **{fp1} / {fp2}** |"
    )
    a("")
    found1 = {(r.name, k) for r in results for k in r.r1}
    found2 = {(r.name, k) for r in results for k in r.r2}
    a(f"- 확정 위험 중 R1이 놓친 것: {_keys(CONFIRMED_RISKY - found1)}")
    a(f"- 확정 위험 중 R2가 놓친 것: {_keys(CONFIRMED_RISKY - found2)}")
    a(f"- 제외(목록만): {', '.join(excluded) or '없음'}")
    a("")
    fp_small = fp_other = 0
    for r in results:
        for k in r.r1:
            if (r.name, k) in CONFIRMED_RISKY:
                continue
            hit = [h.sentence for kk, h in r.hits if kk == k and h.rule in ("R1", "R2")]
            if all(SMALL_SUM.search(s) for s in hit):
                fp_small += 1
            else:
                fp_other += 1
    a("## 2. 고르는 기준 적용 (규칙 문서 4절 순서)")
    a("")
    a("| 규칙 | 1. 확정 위험 탐지(/8) | 2. 오탐(답변) | 3. 단순함 | 결과 |")
    a("|---|---|---|---|---|")
    pick = "R1" if det1 == total else ("R2" if det2 == total else "")
    for name, det, fp, size in (("R1", det1, fp1, 3), ("R2", det2, fp2, 4)):
        verdict = "선택" if pick == name else ("기준 1 미달" if det < total else "-")
        a(f"| {name} | {det} | {fp} | 신호 {size}개 | {verdict} |")
    a("")
    a(
        f"- R1 오탐 {fp1}답변 중 걸린 문장이 모두 소액해외송금업 한도(제2-31조) 문장인 답변 "
        f"{fp_small}, 그 밖 {fp_other}(분류 보조, 판정 아님)."
    )
    a("")
    a("## 3. 걸린 문장 전부")
    a("")
    a("'사람 판정'은 판정 파일 값이다. 머지 재측정은 리뷰 문서 answerable 표를 읽었고,")
    a("회귀·머지 not_in_docs처럼 판정이 없는 곳은 '-'다.")
    a("'오탐 아님 후보'는 확정 위험 밖에서 걸렸지만 문장 내용이 틀렸을 수 있는 것이다")
    a("(Claude Code 표시, 분류는 사람).")
    a("")
    a("| 파일 | 문항#회차 | 규칙 | 확정 위험 | 사람 판정 | 오탐 아님 후보 | 문장 |")
    a("|---|---|---|---|---|---|---|")
    for r in results:
        g = grades.get(r.name, {})
        for key, h in r.hits:
            conf = "예" if (r.name, key) in CONFIRMED_RISKY else "아니오"
            note = notes.get(f"{r.name}|{key}", "")
            a(
                f"| {r.name} | {key} | {h.rule} | {conf} | {g.get(key, '-')} "
                f"| {_cell(note)} | {_cell(h.sentence)} |"
            )
    return "\n".join(o) + "\n"


def render_strict(
    results: Sequence[FileResult],
    grades: Mapping[str, Mapping[str, str]],
    excluded: Sequence[str],
    notes: Mapping[str, str],
    rule: str = "v2",
) -> str:
    """v2·v3 결과. 통과 기준(상한 포함): 탐지 8/8, 오탐 0, 혼합 미검출."""
    spec = STRICT[rule]
    hit_name = str(spec["hit"])
    has_d = spec["has_d"]
    total = len(CONFIRMED_RISKY)
    o: list[str] = []
    a = o.append
    a(f"# 금융 정책 답변 위험 문장 검사 0원 실행 (#93, 규칙 {rule})")
    a("")
    a(f"규칙: `ai/specs/rag/{spec['doc']}`(실행 전 커밋).")
    a("저장된 답변만 읽었다(외부 호출 없음, 서버 코드 변경 없음).")
    a(str(spec["formula"]))
    a(str(spec["disclose"]))
    a("")
    a("## 1. 파일별 결과")
    a("")
    a(f"| 파일 | 답변 수 | {hit_name} 걸림 | 혼합(답변) | 확정 위험 탐지 (/8) | 오탐 |")
    a("|---|---|---|---|---|---|")
    det = fp = 0
    for r in results:
        c = sum((r.name, k) in CONFIRMED_RISKY for k in r.r1)
        det, fp = det + c, fp + len(r.r1) - c
        a(f"| {r.name} | {r.answers} | {len(r.r1)} | {len(r.mixed)} | {c} | {len(r.r1) - c} |")
    n_ans = sum(r.answers for r in results)
    n_hit = sum(len(r.r1) for r in results)
    n_mx = sum(len(r.mixed) for r in results)
    a(f"| **합계** | {n_ans} | {n_hit} | {n_mx} | **{det}** (/{total}) | **{fp}** |")
    a("")
    found = {(r.name, k) for r in results for k in r.r1}
    a(f"- 확정 위험 중 놓친 것: {_keys(CONFIRMED_RISKY - found)}")
    a(f"- 제외(목록만): {', '.join(excluded) or '없음'}")
    a("")
    mixed_hit = 0  # 혼합 문장이 검출 규칙으로 걸린 수(정의상 0이어야 함)
    for r in results:
        mixed_hit += sum(1 for _, h in r.hits if h.rule == hit_name and _any(RECEIVE, h.sentence))
    ok1, ok2, ok3 = det == total, fp == 0, mixed_hit == 0
    a("## 2. 통과 기준 (규칙 문서 4절, 하나라도 어기면 불통과)")
    a("")
    a("| 기준 | 값 | 판정 |")
    a("|---|---|---|")
    a(f"| 1. 확정 위험 탐지 8/8 | {det}/{total} | {'통과' if ok1 else '불통과'} |")
    a(f"| 2. 오탐 0(상한 0) | {fp} | {'통과' if ok2 else '불통과'} |")
    a(f"| 3. 혼합 문장 미검출 | 혼합이 걸린 문장 {mixed_hit} | {'통과' if ok3 else '불통과'} |")
    a(f"| **종합** | | **{'통과' if ok1 and ok2 and ok3 else '불통과'}** |")
    a("")
    a(f"## 3. 걸린 문장 전부 ({hit_name})")
    a("")
    a("| 파일 | 문항#회차 | 확정 위험 | 사람 판정 | 메모 | 문장 |")
    a("|---|---|---|---|---|---|")
    for r in results:
        g = grades.get(r.name, {})
        for key, h in r.hits:
            if h.rule != hit_name:
                continue
            conf = "예" if (r.name, key) in CONFIRMED_RISKY else "아니오"
            note = notes.get(f"{r.name}|{key}", "")
            a(
                f"| {r.name} | {key} | {conf} | {g.get(key, '-')} "
                f"| {_cell(note)} | {_cell(h.sentence)} |"
            )
    a("")
    a(f"## 4. 참고: 혼합 문장 중 {spec['d']}가 있는 것 (검출 아님)")
    a("")
    a("| 파일 | 문항#회차 | 문장 |")
    a("|---|---|---|")
    for r in results:
        for key, h in r.hits:
            if h.rule == "mixed" and has_d(h.sentence):  # type: ignore[operator]
                a(f"| {r.name} | {key} | {_cell(h.sentence)} |")
    return "\n".join(o) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="금융 정책 답변 위험 문장 검사 0원 기준선")
    ap.add_argument("inputs", nargs="+", type=Path)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--excluded", nargs="*", default=[], help="목록에만 적는 제외 파일")
    ap.add_argument("--notes", type=Path, help="'파일|문항#회차' → 오탐 아님 후보 메모(JSON)")
    ap.add_argument("--rule", choices=sorted(CLASSIFIERS), default="v1", help="규칙 버전")
    args = ap.parse_args(argv)
    ai_root = Path(__file__).resolve().parents[2]
    results = [check_file(p, args.rule) for p in args.inputs]
    grades = {r.name: load_grades(ai_root, r.name) for r in results}
    notes = json.loads(args.notes.read_text(encoding="utf-8")) if args.notes else {}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.rule in STRICT:
        text = render_strict(results, grades, args.excluded, notes, args.rule)
    else:
        text = render(results, grades, args.excluded, notes)
    args.output.write_text(text, encoding="utf-8", newline="\n")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

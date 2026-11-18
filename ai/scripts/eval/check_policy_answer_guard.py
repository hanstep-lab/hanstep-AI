"""금융 정책 답변 위험 문장 검사 0원 기준선(#93): 저장된 evaluate_chat jsonl → md.

규칙은 ai/specs/rag/finance_policy_answer_guard_v1.md에 실행 전에 고정했다.
제4-4조 ④(건당 미화 5천불 이내 증빙 없이 '수령')를 '송금'으로 옮긴 문장을 찾는다.

- M(금액 USD 5,000) ∧ S(송금 방향) ∧ ¬R(수령 방향) = R1, R1 ∧ D(무증빙) = R2,
  M ∧ S ∧ R = 혼합(따로 셈). 한 답변에 걸린 문장이 하나라도 있으면 그 답변이 걸린 것이다.
- 확정 위험 8건(설계 문서 17-3절 7건 + 머지 재측정 A03#1)을 몇 개 잡는지,
  그 밖에서 몇 개 걸리는지(오탐) 센다.
- 외부 호출 없음. 서버 코드를 import하지 않는다.

    uv run python scripts/eval/check_policy_answer_guard.py \\
        artifacts/chat/policy_after_v4_2.jsonl ... --output result.md [--notes notes.json]
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_I = re.IGNORECASE
MONEY = [
    re.compile(r"(?<![\d,.])(?:US\$|USD\s?|\$)?\s?5,000(?![\d]|,\d)", _I),
    re.compile(r"(?<![\d,.])(?:US\$|USD\s?|\$)?\s?5000(?!\d)", _I),
    re.compile(r"\b5k\b", _I),
    re.compile(r"\bfive\s+thousand\b", _I),
    re.compile(r"5천\s?(?:달러|불)", _I),
]
SEND = [
    re.compile(r"\b(?:send|sends|sending|sent)\b", _I),
    re.compile(r"\btransfer\w*", _I),
    re.compile(r"\bremit\w*", _I),
    re.compile(r"\bwire\w*", _I),
    re.compile(r"보내"),
    re.compile(r"송금"),
]
RECEIVE = [
    re.compile(r"\breceiv\w*", _I),
    re.compile(r"\bincoming\b", _I),
    re.compile(r"\binbound\b", _I),
    re.compile(r"받"),
    re.compile(r"수령"),
]
NO_DOCS = [
    re.compile(r"\bwithout\s+(?:supporting\s+)?(?:documents?|proof|evidence)\b", _I),
    re.compile(r"\bno\s+documents?\b", _I),
    re.compile(r"증빙\s?없이"),
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


def check_answer(answer: str) -> list[Hit]:
    return [Hit(s, r) for s in sentences(answer) if (r := classify(s)) is not None]


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


def check_rows(name: str, rows: Iterable[Mapping[str, Any]]) -> FileResult:
    res = FileResult(name)
    for row in rows:
        if row.get("type") != "call" or row.get("warmup"):
            continue
        res.answers += 1
        key = f"{row['questionId']}#{row['repeat']}"
        hits = check_answer(answer_text(row))
        rules = {h.rule for h in hits}
        if rules & {"R1", "R2"}:
            res.r1.append(key)
        if "R2" in rules:
            res.r2.append(key)
        if "mixed" in rules:
            res.mixed.append(key)
        res.hits.extend((key, h) for h in hits)
    return res


def check_file(path: Path) -> FileResult:
    lines = path.read_text(encoding="utf-8").splitlines()
    return check_rows(path.name, (json.loads(line) for line in lines if line.strip()))


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


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="금융 정책 답변 위험 문장 검사 0원 기준선")
    ap.add_argument("inputs", nargs="+", type=Path)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--excluded", nargs="*", default=[], help="목록에만 적는 제외 파일")
    ap.add_argument("--notes", type=Path, help="'파일|문항#회차' → 오탐 아님 후보 메모(JSON)")
    args = ap.parse_args(argv)
    ai_root = Path(__file__).resolve().parents[2]
    results = [check_file(p) for p in args.inputs]
    grades = {r.name: load_grades(ai_root, r.name) for r in results}
    notes = json.loads(args.notes.read_text(encoding="utf-8")) if args.notes else {}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    text = render(results, grades, args.excluded, notes)
    args.output.write_text(text, encoding="utf-8", newline="\n")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

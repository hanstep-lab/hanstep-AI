"""금융 정책 RAG(#88) 답변 채점 보조: evaluate_chat.py 결과 jsonl → 자동 신호 + 사람 확인 표(md).

주 지표(답변 정답률: correct / partial / wrong / correct_abstain, 위험 오답)는 사람이 정한다.
이 스크립트는 판단을 돕는 자동 신호만 낸다(API 호출 없음).

- 핵심 사실 힌트: 질문 세트 keyFacts[].answerHint 정규식이 답변에 있는지(있어도 정답 판정이 아님).
- 조문 인용 수: 'Article 4-4', '제4-4조', 법령 이름 같은 인용 표현 수.
- 시행일 표시: 'effective', 'in force', '시행' 같은 표현이 있는지.
- 문서에 없다는 안내(not_in_docs): 'not in', 'don't have', 'no information' 같은 표현이 있는지.
- 근거 없는 숫자: evaluate_chat의 numberCheck.unsupported(도구 결과에 없는 숫자).
- 단정 표현: 'is illegal', 'you are a resident' 같은 법률 판단 단정 후보(사람 확인).

    uv run python scripts/finance_policy_review.py artifacts/chat/policy_baseline_v4_1.jsonl \\
        --output artifacts/chat/policy_baseline_v4_1_review.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import _bootstrap

QUESTIONS_PATH = _bootstrap.AI_ROOT / "specs" / "rag" / "finance_policy_questions_v1.json"
_I = re.IGNORECASE
CITATION = re.compile(
    r"\bArticle\s+\d+(?:-\d+)?|제\s?\d+(?:-\d+)?조|Foreign\s+Exchange\s+(?:Transactions?\s+)?"
    r"(?:Act|Regulation)|Electronic\s+Financial\s+Transactions\s+Act|Employment\s+Permit|"
    r"Act\s+on\s+the\s+Employment\s+of\s+Foreign\s+Workers|외국환거래|전자금융거래법",
    _I,
)
EFFECTIVE = re.compile(r"\beffective\b|\bin\s+force\b|\bas\s+of\s+\w+\s+\d|시행", _I)
NOT_IN_DOCS = re.compile(
    r"\bnot\s+(?:in|covered|included)\b|\bdon(?:'|’)?t\s+have\b|\bno\s+information\b|"
    r"\bcan(?:'|’)?t\s+(?:find|confirm|check)\b|\bunable\s+to\s+(?:find|confirm)\b",
    _I,
)
ASSERTIVE = re.compile(
    r"\bis\s+(?:clearly\s+)?illegal\b|\byou\s+(?:are|will\s+be)\s+(?:a\s+)?(?:non-)?resident\b|"
    r"\byou\s+will\s+be\s+(?:punished|fined|jailed)\b|\byou\s+(?:are|were)\s+breaking\b",
    _I,
)


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return [r for r in rows if r.get("type") == "call" and not r.get("warmup")]


def signals(question: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    answer = row.get("answer") or ""
    facts = question.get("keyFacts") or []
    hints = [bool(re.search(f["answerHint"], answer, _I)) for f in facts if f.get("answerHint")]
    tools = [t.get("toolName") for t in row.get("tools") or []]
    unsupported = (row.get("numberCheck") or {}).get("unsupported") or []
    return {
        "factHints": hints,
        "citations": len(CITATION.findall(answer)),
        "effectiveDate": bool(EFFECTIVE.search(answer)),
        "saysNotInDocs": bool(NOT_IN_DOCS.search(answer)),
        "unsupportedNumbers": list(unsupported),
        "assertive": [m.group(0) for m in ASSERTIVE.finditer(answer)],
        "tools": tools,
        "toolMatch": row.get("toolMatch"),
    }


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", "<br>")


def render(qset: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> str:
    questions = {q["id"]: q for q in qset["questions"]}
    scored = [(questions[r["questionId"]], r, signals(questions[r["questionId"]], r)) for r in rows]
    by_category: Counter[str] = Counter(q["category"] for q, _, _ in scored)
    out = [
        "# 금융 정책 질문 답변 확인표",
        "",
        "자동 신호는 판단 보조다. 정답·부분·오답·올바른 기권·위험 오답은 "
        "사람이 '판정' 칸에 적는다.",
        f"회차: {len(rows)} ({', '.join(f'{k} {v}' for k, v in sorted(by_category.items()))})",
        "",
        "## 요약(자동 신호)",
        "",
        "| 항목 | 값 |",
        "|---|---|",
    ]
    answerable = [(q, r, s) for q, r, s in scored if q["category"] == "answerable"]
    not_in_docs = [(q, r, s) for q, r, s in scored if q["category"] == "not_in_docs"]
    out_of_scope = [(q, r, s) for q, r, s in scored if q["category"] == "out_of_scope"]
    all_hints = sum(all(s["factHints"]) for _, _, s in answerable if s["factHints"])
    out += [
        f"| answerable: 핵심 사실 힌트가 모두 있는 회차 | {all_hints}/{len(answerable)} |",
        f"| answerable: 조문 인용이 하나라도 있는 회차 | "
        f"{sum(s['citations'] > 0 for _, _, s in answerable)}/{len(answerable)} |",
        f"| 시행일 표시가 있는 회차(전체) | "
        f"{sum(s['effectiveDate'] for _, _, s in scored)}/{len(scored)} |",
        f"| not_in_docs: 문서에 없다는 표현 | "
        f"{sum(s['saysNotInDocs'] for _, _, s in not_in_docs)}/{len(not_in_docs)} |",
        f"| not_in_docs: 숫자가 있는 회차 | "
        f"{sum(bool(s['unsupportedNumbers']) for _, _, s in not_in_docs)}/{len(not_in_docs)} |",
        f"| 근거 없는 숫자가 있는 회차(전체) | "
        f"{sum(bool(s['unsupportedNumbers']) for _, _, s in scored)}/{len(scored)} |",
        f"| 단정 표현 후보가 있는 회차 | "
        f"{sum(bool(s['assertive']) for _, _, s in scored)}/{len(scored)} |",
        f"| out_of_scope: 기대 도구와 일치 | "
        f"{sum(bool(s['toolMatch']) for _, _, s in out_of_scope)}/{len(out_of_scope)} |",
        "",
        "## 문항별",
        "",
        "| 문항 | 회 | 분류 | 핵심 사실(힌트 O/X) | 위험 오답 유형 | 답변 | 인용 | 시행일 "
        "| 근거 없는 숫자 | 도구 | 판정 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for q, r, s in scored:
        facts = "<br>".join(
            f"{'O' if hit else 'X'} {f['fact']}"
            for f, hit in zip(q.get("keyFacts") or [], s["factHints"], strict=False)
        )
        if q["category"] == "not_in_docs":
            facts = ("O" if s["saysNotInDocs"] else "X") + " 문서에 없다는 안내"
        risky = "<br>".join(q.get("riskyWrong") or []) or "–"
        cells = [
            q["id"],
            str(r.get("repeat", "")),
            q["category"],
            facts or "–",
            risky,
            r.get("answer") or "",
            str(s["citations"]),
            "O" if s["effectiveDate"] else "X",
            ", ".join(map(str, s["unsupportedNumbers"])) or "–",
            ", ".join(t or "" for t in s["tools"]) or "–",
            "",
        ]
        out.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    return "\n".join(out) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("results", type=Path, help="evaluate_chat.py 결과 jsonl")
    parser.add_argument("--questions", type=Path, default=QUESTIONS_PATH)
    parser.add_argument("--output", type=Path, required=True, help="확인표 md 경로")
    args = parser.parse_args(argv)
    qset = json.loads(args.questions.read_text(encoding="utf-8"))
    rows = load_rows(args.results)
    known = {q["id"] for q in qset["questions"]}
    unknown = sorted({r["questionId"] for r in rows} - known)
    if unknown:
        print(f"FAILED: question ids not in {args.questions.name}: {unknown}")
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(qset, rows), encoding="utf-8")
    print(f"wrote {args.output} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    raise SystemExit(main())

"""금융 정책 RAG(#88) 답변 채점 보조: evaluate_chat.py 결과 jsonl → 자동 신호 + 사람 확인 표(md).

주 지표(답변 정답률)는 사람이 정한다: correct / partial / wrong / risky_wrong /
abstain(answerable에서 틀린 내용 없이 답을 못 함) / correct_abstain(not_in_docs).
--grades로 사람 판정 파일을 주면 등급을 표에 채우고
분류·회차·variant(original/user)별로 집계한다. 판정 파일 형식(키는 '문항#회차'):

    {"A01#1": {"grade": "abstain", "reason": "…", "outOfDocsClaims": 0, "assertive": 0,
               "targetError": 0, "abstainCause": "tool_failed"}}

자동 신호(판단 보조, API 호출 없음):
- 핵심 사실 힌트: keyFacts[].answerHint 정규식이 답변에 있는지(있어도 정답 판정이 아님).
- 법령 이름 언급 수와 조문 번호 인용 수를 따로 센다.
- 시행일 표시('effective', 'in force', '시행' 등), 문서에 없다는 안내(not_in_docs).
- 근거 없는 숫자: evaluate_chat의 numberCheck.unsupported에서
  상담 전화번호(112·1332·126·1345)를 뺀 것.
- 단정 표현 후보: 'is illegal', 'against the law', 'prohibited', 'not allowed' 등(사람 확인).
- 실행 기록: 입력·출력 토큰, 첫 글자까지 시간, 전체 응답 시간, LLM 호출 수, 도구별 상태·오류 코드,
  모델이 만든 검색어 원문(도구 인자의 query).

    uv run python scripts/finance_policy_review.py artifacts/chat/policy_baseline_v4_1.jsonl \\
        --output artifacts/chat/policy_baseline_v4_1_review.md [--grades grades.json]
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
# 상담 전화번호(답변이 안내해도 규정 숫자로 치지 않는다).
HELPLINES = frozenset({"112", "1332", "126", "1345"})
LAW_NAME = re.compile(
    r"Foreign\s+Exchange\s+(?:Transactions?\s+)?(?:Act|Regulation)|"
    r"Electronic\s+Financial\s+Transactions\s+Act|electronic\s+finance\s+law|"
    r"Employment\s+Permit|Act\s+on\s+the\s+Employment\s+of\s+Foreign\s+Workers|"
    r"외국환거래(?:법|규정)|전자금융거래법|외국인고용법",
    _I,
)
ARTICLE = re.compile(r"\bArticle\s+\d+(?:-\d+)?|제\s?\d+(?:-\d+)?조", _I)
EFFECTIVE = re.compile(r"\beffective\b|\bin\s+force\b|\bas\s+of\s+\w+\s+\d|시행", _I)
NOT_IN_DOCS = re.compile(
    r"\bnot\s+(?:in|covered|included)\b|\bdon(?:'|’)?t\s+have\b|\bno\s+information\b|"
    r"\bcan(?:'|’)?t\s+(?:find|confirm|check|see|give)\b|\bunable\s+to\s+(?:find|confirm)\b",
    _I,
)
ASSERTIVE = re.compile(
    r"\bis\s+(?:clearly\s+)?illegal\b|\bagainst\s+the\s+law\b|\b(?:is|are)\s+prohibited\b|"
    r"\bis\s+not\s+allowed\b|\byou\s+(?:are|will\s+be)\s+(?:a\s+)?(?:non-)?resident\b|"
    r"\byou\s+will\s+be\s+(?:punished|fined|jailed)\b|\byou\s+(?:are|were)\s+breaking\b",
    _I,
)
GRADES = ("correct", "partial", "wrong", "risky_wrong", "abstain", "correct_abstain", "ok")
CATEGORIES = ("answerable", "not_in_docs", "out_of_scope")


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return [r for r in rows if r.get("type") == "call" and not r.get("warmup")]


def row_key(row: Mapping[str, Any]) -> str:
    return f"{row['questionId']}#{row.get('repeat', 1)}"


def signals(question: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    answer = row.get("answer") or ""
    facts = question.get("keyFacts") or []
    hints = [bool(re.search(f["answerHint"], answer, _I)) for f in facts if f.get("answerHint")]
    tools = row.get("tools") or []
    unsupported = (row.get("numberCheck") or {}).get("unsupported") or []
    usage = row.get("usage") or {}
    timings = row.get("timings") or {}
    return {
        "factHints": hints,
        "lawNames": len(LAW_NAME.findall(answer)),
        "articles": len(ARTICLE.findall(answer)),
        "effectiveDate": bool(EFFECTIVE.search(answer)),
        "saysNotInDocs": bool(NOT_IN_DOCS.search(answer)),
        "unsupportedNumbers": [n for n in unsupported if str(n) not in HELPLINES],
        "helplineNumbers": [n for n in unsupported if str(n) in HELPLINES],
        "assertive": [m.group(0) for m in ASSERTIVE.finditer(answer)],
        "tools": [
            {"name": t.get("toolName"), "status": t.get("status"), "errorCode": t.get("errorCode")}
            for t in tools
        ],
        "queries": [
            str((t.get("arguments") or {}).get("query"))
            for t in tools
            if (t.get("arguments") or {}).get("query")
        ],
        "toolMatch": row.get("toolMatch"),
        "inputTokens": usage.get("inputTokens"),
        "outputTokens": usage.get("outputTokens"),
        "llmCalls": usage.get("llmCalls"),
        "firstDeltaMs": timings.get("firstDeltaMs"),
        "doneMs": timings.get("doneMs"),
        "answerChars": len(answer),
    }


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", "<br>")


def _count(items: Sequence[Mapping[str, Any]]) -> str:
    counter = Counter(i["grade"] for i in items)
    return " | ".join(str(counter.get(k, 0)) for k in GRADES)


def grade_tables(
    scored: Sequence[tuple[Mapping[str, Any], Mapping[str, Any], Any]], grades: Mapping[str, Any]
) -> list[str]:
    """사람 판정 집계: 분류×회차, 분류×variant, 보조 지표."""
    items = []
    for q, r, _ in scored:
        g = grades.get(row_key(r))
        if g is None:
            continue
        items.append(
            {
                **g,
                "category": q["category"],
                "variant": q.get("variant", "original"),
                "repeat": r.get("repeat", 1),
            }
        )
    if not items:
        return []
    out = ["", "## 주 지표(사람 판정)", "", f"판정된 회차: {len(items)}/{len(scored)}", ""]
    out += ["| 분류 | 묶음 | " + " | ".join(GRADES) + " |", "|---|---|" + "---|" * len(GRADES)]
    for category in CATEGORIES:
        mine = [i for i in items if i["category"] == category]
        if not mine:
            continue
        for repeat in sorted({i["repeat"] for i in mine}):
            rows = [i for i in mine if i["repeat"] == repeat]
            out.append(f"| {category} | 회차 {repeat} | {_count(rows)} |")
        for variant in sorted({i["variant"] for i in mine}):
            rows = [i for i in mine if i["variant"] == variant]
            out.append(f"| {category} | variant {variant} | {_count(rows)} |")
        out.append(f"| {category} | 합 | {_count(mine)} |")
    out += [
        "",
        "| 보조(사람 판정) | 값 |",
        "|---|---|",
        f"| 문서 범위 밖 일반 진술 수 | {sum(int(i.get('outOfDocsClaims', 0)) for i in items)} |",
        f"| 법률 판단 단정 수 | {sum(int(i.get('assertive', 0)) for i in items)} |",
        f"| 적용 대상 오류 수 | {sum(int(i.get('targetError', 0)) for i in items)} |",
    ]
    causes = Counter(i.get("abstainCause") for i in items if i["grade"] == "abstain")
    if causes:
        listed = ", ".join(f"{k or '미기록'} {v}" for k, v in causes.most_common())
        out.append(f"| 기권(답 못 함) 원인 | {listed} |")
    return out


def _tool_label(tool: Mapping[str, Any]) -> str:
    status = tool["status"]
    return f"{status} {tool['errorCode']}" if status == "FAILED" else str(status)


def render(
    qset: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], grades: Mapping[str, Any]
) -> str:
    questions = {q["id"]: q for q in qset["questions"]}
    scored = [(questions[r["questionId"]], r, signals(questions[r["questionId"]], r)) for r in rows]
    by_category: Counter[str] = Counter(q["category"] for q, _, _ in scored)
    answerable = [(q, r, s) for q, r, s in scored if q["category"] == "answerable"]
    not_in_docs = [(q, r, s) for q, r, s in scored if q["category"] == "not_in_docs"]
    out_of_scope = [(q, r, s) for q, r, s in scored if q["category"] == "out_of_scope"]
    tool_stats: Counter[tuple[str, str]] = Counter()
    for _, _, s in scored:
        for t in s["tools"]:
            tool_stats[(str(t["name"]), _tool_label(t))] += 1
    n_ans, n_nid, n_all = len(answerable), len(not_in_docs), len(scored)
    tool_text = ", ".join(f"{n} {st} {c}" for (n, st), c in sorted(tool_stats.items())) or "없음"
    out = [
        "# 금융 정책 질문 답변 확인표",
        "",
        "자동 신호는 판단 보조다. 정답·부분·오답·위험 오답·기권(답 못 함)·올바른 기권은 "
        "사람이 정한다.",
        f"회차: {n_all} ({', '.join(f'{k} {v}' for k, v in sorted(by_category.items()))})",
        "",
        "## 요약(자동 신호)",
        "",
        "| 항목 | 값 |",
        "|---|---|",
    ]
    for variant in ("original", "user"):
        mine = [s for q, _, s in answerable if q.get("variant", "original") == variant]
        if mine:
            hit = sum(all(s["factHints"]) for s in mine if s["factHints"])
            out.append(
                f"| answerable({variant}): 핵심 사실 힌트가 모두 있는 회차 | {hit}/{len(mine)} |"
            )
    law = sum(s["lawNames"] > 0 for _, _, s in answerable)
    art = sum(s["articles"] > 0 for _, _, s in answerable)
    eff = sum(s["effectiveDate"] for _, _, s in scored)
    nid = sum(s["saysNotInDocs"] for _, _, s in not_in_docs)
    nid_num = sum(bool(s["unsupportedNumbers"]) for _, _, s in not_in_docs)
    any_num = sum(bool(s["unsupportedNumbers"]) for _, _, s in scored)
    asrt = sum(bool(s["assertive"]) for _, _, s in scored)
    match = sum(bool(s["toolMatch"]) for _, _, s in out_of_scope)
    out += [
        f"| answerable: 법령 이름 언급 회차 | {law}/{n_ans} |",
        f"| answerable: 조문 번호 인용 회차 | {art}/{n_ans} |",
        f"| 시행일 표시가 있는 회차(전체) | {eff}/{n_all} |",
        f"| not_in_docs: 문서에 없다는 표현 | {nid}/{n_nid} |",
        f"| not_in_docs: 숫자가 있는 회차(전화번호 제외) | {nid_num}/{n_nid} |",
        f"| 근거 없는 숫자가 있는 회차(전화번호 제외, 전체) | {any_num}/{n_all} |",
        f"| 단정 표현 후보가 있는 회차 | {asrt}/{n_all} |",
        f"| out_of_scope: 기대 도구와 일치 | {match}/{len(out_of_scope)} |",
        f"| 도구 호출(도구·상태별) | {tool_text} |",
    ]
    out += grade_tables(scored, grades)
    out += [
        "",
        "## 문항별",
        "",
        "| 문항 | 회 | 분류 | variant | 핵심 사실(힌트 O/X) | 위험 오답 유형 | 답변 | 법령명/조문 "
        "| 시행일 | 근거 없는 숫자 | 도구(상태) | 검색어 | 토큰 in/out | LLM | 첫 글자/전체 ms "
        "| 판정 | 이유 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for q, r, s in scored:
        facts = "<br>".join(
            f"{'O' if hit else 'X'} {f['fact']}"
            for f, hit in zip(q.get("keyFacts") or [], s["factHints"], strict=False)
        )
        if q["category"] == "not_in_docs":
            facts = ("O" if s["saysNotInDocs"] else "X") + " 문서에 없다는 안내"
        grade = grades.get(row_key(r)) or {}
        tools = ", ".join(f"{t['name']}({_tool_label(t)})" for t in s["tools"])
        cells = [
            q["id"],
            r.get("repeat", ""),
            q["category"],
            q.get("variant", "original"),
            facts or "–",
            "<br>".join(q.get("riskyWrong") or []) or "–",
            r.get("answer") or "",
            f"{s['lawNames']}/{s['articles']}",
            "O" if s["effectiveDate"] else "X",
            ", ".join(map(str, s["unsupportedNumbers"])) or "–",
            tools or "–",
            " / ".join(s["queries"]) or "–",
            f"{s['inputTokens']}/{s['outputTokens']}",
            s["llmCalls"],
            f"{s['firstDeltaMs']}/{s['doneMs']}",
            grade.get("grade", ""),
            grade.get("reason", ""),
        ]
        out.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    return "\n".join(out) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("results", type=Path, help="evaluate_chat.py 결과 jsonl")
    parser.add_argument("--questions", type=Path, default=QUESTIONS_PATH)
    parser.add_argument("--grades", type=Path, default=None, help="사람 판정 JSON(키 '문항#회차')")
    parser.add_argument("--output", type=Path, required=True, help="확인표 md 경로")
    args = parser.parse_args(argv)
    qset = json.loads(args.questions.read_text(encoding="utf-8"))
    rows = load_rows(args.results)
    known = {q["id"] for q in qset["questions"]}
    unknown = sorted({r["questionId"] for r in rows} - known)
    if unknown:
        print(f"FAILED: question ids not in {args.questions.name}: {unknown}")
        return 1
    grades: dict[str, Any] = {}
    if args.grades is not None:
        grades = json.loads(args.grades.read_text(encoding="utf-8"))
        bad = sorted(k for k, v in grades.items() if v.get("grade") not in GRADES)
        if bad:
            print(f"FAILED: unknown grade in {bad}; choose from {list(GRADES)}")
            return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(qset, rows, grades), encoding="utf-8")
    print(f"wrote {args.output} ({len(rows)} rows, {len(grades)} grades)")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    raise SystemExit(main())

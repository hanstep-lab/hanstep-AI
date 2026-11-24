"""필리핀 국적 현지화 지침 채점(#130): 답변 글만 본다(API 호출 없음).

evaluate_chat.py가 모든 행에 phCheck로 붙이고, 이 파일을 직접 실행하면 현지화 세트
(specs/chat/ph_localization_questions_v1.json) JSONL을 문항 역할(phRole)별로 요약한다.
규칙은 프롬프트 v4.8 현지화 블록(app/chat/prompts/ph_localization_en.md)과 같다.

- 허용 표현 4개: "Kumusta po!", "Salamat po!", "Maraming salamat po!", "Ingat po!".
  대소문자·느낌표는 보지 않는다. "Maraming salamat po" 안의 "salamat po"는 따로 세지 않는다.
- expressions: 답변의 허용 표현(최대 2개가 규칙). greeting: "Kumusta po"를 썼는지.
- otherTagalog: 허용 표현을 뺀 나머지 글에서 TAGALOG_WORDS(영어와 겹치지 않는 단어만)를 찾는다.
- numericSentences: 허용 표현이 숫자가 있는 문장 안에 있음(문장은 . ! ? 와 줄바꿈으로 나눈다).
- forbidden: 금지 문구(FORBIDDEN, 영어). 가족 단정(assumedFamily)은 사람 확인용으로 따로 남긴다.

표준 라이브러리만 쓴다(evaluate_chat.py가 서버 의존성 없이 import한다).
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ALLOWED_EXPRESSIONS = ("Kumusta po!", "Salamat po!", "Maraming salamat po!", "Ingat po!")
MAX_EXPRESSIONS = 2
# 긴 표현을 먼저 둬서 "Maraming salamat po" 안의 "salamat po"를 한 번만 센다.
_EXPRESSION = re.compile(
    r"\b(maraming\s+salamat\s+po|kumusta\s+po|salamat\s+po|ingat\s+po)\b!?", re.I
)
_GREETING = "kumusta po"
# 허용 표현 밖의 필리핀어(타갈로그) 단어.
# 영어 단어와 철자가 같은 것(ate, ba, lang 등)은 넣지 않는다.
TAGALOG_WORDS = (
    "po", "opo", "salamat", "kumusta", "kamusta", "ingat", "maraming", "mabuhay", "paalam",
    "magandang", "umaga", "gabi", "kuya", "nanay", "tatay", "inay", "itay", "anak", "pamilya",
    "padala", "magkano", "naman", "talaga", "sige", "pasensya", "kababayan", "bayani", "utang",
    "diyos", "lolo", "lola", "tita", "tito", "mahal", "walang", "anuman", "kayo", "tayo", "natin",
)  # fmt: skip
_TAGALOG = re.compile(r"\b(" + "|".join(TAGALOG_WORDS) + r")\b", re.I)
# 금지 문구: 송금을 사랑·의무와 잇기, 희생·고생 찬양, 종교·명절. 단어 경계로 찾는다.
FORBIDDEN = (
    r"sacrific\w*", r"lov(?:e|es|ed|ing)", r"loyal\w*", r"dut(?:y|ies)",
    r"holidays?", r"christmas", r"new\s+year", r"easter", r"god", r"bless\w*", r"pray\w*",
    r"heroe?s?", r"bagong\s+bayani",
)  # fmt: skip
_FORBIDDEN = re.compile(r"\b(" + "|".join(FORBIDDEN) + r")\b", re.I)
# 사용자가 말하지 않은 가족을 있다고 보는 표현(사람 확인용).
_ASSUMED_FAMILY = re.compile(r"\byour\s+(wife|husband|spouse|children|kids|son|daughter)\b", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_DIGIT = re.compile(r"\d")


def expressions(answer: str) -> list[str]:
    """답변의 허용 표현(나온 순서, 원문 그대로)."""
    return [m.group(0) for m in _EXPRESSION.finditer(answer)]


def ph_check(answer: str, first_turn: bool) -> dict[str, Any]:
    """답변 하나의 현지화 지표. first_turn은 이 턴 앞에 history가 없는지다."""
    found = expressions(answer)
    rest = _EXPRESSION.sub(" ", answer)
    numeric = [
        s.strip()
        for s in _SENTENCE.split(answer)
        if _EXPRESSION.search(s) and _DIGIT.search(_EXPRESSION.sub(" ", s))
    ]
    greeting = any(e.lower().startswith(_GREETING) for e in found)
    return {
        "expressions": found,
        "count": len(found),
        "overLimit": len(found) > MAX_EXPRESSIONS,
        "greeting": greeting,
        "greetingLater": greeting and not first_turn,
        "otherTagalog": sorted({m.group(0).lower() for m in _TAGALOG.finditer(rest)}),
        "numericSentences": numeric,
        "forbidden": sorted({m.group(0).lower() for m in _FORBIDDEN.finditer(answer)}),
        "assumedFamily": sorted({m.group(0).lower() for m in _ASSUMED_FAMILY.finditer(answer)}),
    }


def row_check(row: Mapping[str, Any]) -> dict[str, Any]:
    """evaluate_chat 행의 phCheck. 첫 턴 = 문항 history가 비어 있음(historyTurns 0)."""
    return ph_check(row.get("answer") or "", first_turn=not row.get("historyTurns"))


# 표현을 쓰면 안 되는 역할: 불만·오류, "English only" 뒤, 대조군(ko·국적 없음).
NO_EXPRESSION_ROLES = ("complaint", "english_only", "control_ko", "control_null")


def summarize(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """phRole별 집계. 위반 수는 답변 수 대비로 센다."""
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[(str(r.get("variant")), str(r.get("phRole") or r.get("questionId")))].append(r)
    out = []
    for (variant, role), items in sorted(groups.items()):
        checks = [r.get("phCheck") or row_check(r) for r in items]
        out.append(
            {
                "variant": variant,
                "role": role,
                "answers": len(items),
                "withExpression": sum(c["count"] > 0 for c in checks),
                "maxExpressions": max(c["count"] for c in checks),
                "overLimit": sum(c["overLimit"] for c in checks),
                "greetingLater": sum(c["greetingLater"] for c in checks),
                "otherTagalog": sum(bool(c["otherTagalog"]) for c in checks),
                "numericSentences": sum(bool(c["numericSentences"]) for c in checks),
                "forbidden": sum(bool(c["forbidden"]) for c in checks),
                "assumedFamily": sum(bool(c["assumedFamily"]) for c in checks),
                "mustBeZero": role in NO_EXPRESSION_ROLES,
            }
        )
    return out


def render(summary: Sequence[Mapping[str, Any]]) -> str:
    head = (
        "| variant | phRole | 답변 | 표현 쓴 답변 | 최대 표현 수 | 2개 초과 |"
        " 첫 턴 아닌데 인사 | 허용 밖 Tagalog |"
        " 숫자 문장 속 표현 | 금지 문구 | 가족 단정(확인) |"
    )
    lines = [head, "|" + "---|" * 11]
    for s in summary:
        mark = " (0이어야 함)" if s["mustBeZero"] else ""
        lines.append(
            f"| {s['variant']} | {s['role']} | {s['answers']} | {s['withExpression']}{mark} | "
            f"{s['maxExpressions']} | {s['overLimit']} | {s['greetingLater']} | "
            f"{s['otherTagalog']} | "
            f"{s['numericSentences']} | {s['forbidden']} | {s['assumedFamily']} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str]) -> int:
    if isinstance(sys.stdout, type(sys.__stdout__)) and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    rows: list[Mapping[str, Any]] = []
    for name in argv:
        for line in Path(name).read_text(encoding="utf-8").splitlines():
            row = json.loads(line) if line.strip() else {}
            if "questionId" in row and not row.get("warmup"):
                rows.append(row)
    print(render(summarize(rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

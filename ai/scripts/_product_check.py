"""상품 질문 채점(이슈 #65): 거절·상품명·금리 언급을 답변 글로만 판정한다(API 호출 없음).

evaluate_chat.py가 product_questions_v1.json 행(expectedOutcome이 있는 행)에 붙인다.
규칙은 기준선 측정 전에 고정했다(specs/chat/product_tool_v1_results.md 3절).
결과를 보고 규칙을 바꾸면 그 문서 공개 절에 적는다.

- behavior: 조건표 상품명이 있으면 named_products, 아니고 거절 문구가 있으면 declined,
  둘 다 아니면 other.
- 조건표 상품명: 한국어 이름(앞의 '신한 ' 생략 포함)과 영어 별칭. 앱 이름과 겹치는 단독 표기
  ('SOL Global', 'Super SOL')는 상품명으로 치지 않고, 상품을 가리키는 말이 붙은 형태만 센다.
- 조건표 밖 상품명 후보: Shinhan + 대문자 단어 + 상품 종류, 다른 은행 + Savings/Deposit/Account,
  따옴표·굵게 안의 Savings/Deposit/Account. 사람 확인용이다.
- rateValue(위반): % 또는 p.a.가 rate/interest/APR/yield/금리/이자와 같은 문장에 있음.
  rateWord(참고): interest·APR·preferential rate·yield·금리·이자·우대 단어만
  (단어 경계, interested 제외).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

PRODUCT_TOOL = "recommend_savings_products"
_I = re.IGNORECASE

# 조건표 상품 13개(추천 불가 '쏠편한 선물하는 적금' 포함): 한국어 이름 → 영어 별칭 정규식.
# 일반 영어 표현과 겹치는 별칭(smart savings·gift savings·one-month savings)은 (?-i:)로
# 대문자 표기만 세거나 'Shinhan'이 붙은 형태만 센다.
CATALOG_ALIASES: dict[str, tuple[str, ...]] = {
    "SOL글로벌 적금": (r"\bSOL\s?Global\s+(?:installment\s+)?savings\b",),
    "슈퍼SOL 웰컴 정기예금": (r"\bSuper\s?SOL\s+Welcome\b",),
    "신한 My플러스 정기예금": (r"\bMy\s?Plus\s+(?:time\s+|fixed\s+)?deposit\b",),
    "신한 S드림(DREAM)정기예금": (r"\bS[\s-]?Dream\b",),
    "신한 땡겨요 적금": (r"\bTtaeng\w*",),
    "신한 스마트 적금": (
        r"\bShinhan\s+Smart\b",
        r"(?-i:\bSmart\s+(?:Installment\s+)?Savings\b)",
    ),
    "신한 안녕, 반가워 적금": (r"\bAnnyeong\b", r"\bHello,?\s+Nice\s+to\s+Meet\s+You\b"),
    "신한 알.쏠 적금": (r"\bAl\.?\s?Sol\b",),
    "쏠편한 선물하는 적금": (
        r"\bSol\s?pyeonhan\s+gift\w*",
        r"(?-i:\bGift(?:ing)?\s+Savings\b)",
    ),
    "쏠편한 정기예금": (r"\bSol\s?pyeonhan\s+(?:time\s+|fixed\s+)?deposit\b",),
    "정기적금": (),  # 영어로는 일반 명사(installment savings)라 한국어 이름만 센다.
    "청년 처음적금": (r"\bYouth\s+First\s+Savings\b", r"\bCheongnyeon\b"),
    "한 달부터 적금": (r"(?-i:\bOne[-\s]Month\s+(?:Start\s+)?Savings\b)", r"\bHan\s?dal\b"),
}

DECLINE_PATTERNS = (
    r"\bnot\s+(?:yet\s+)?available\b",
    r"\b(?:can(?:'|’)?t|cannot|unable\s+to|not\s+able\s+to)\s+recommend\b",
    r"\b(?:do\s+not|don(?:'|’)?t|can(?:'|’)?t|cannot)\s+provide\b[^.\n]{0,40}\brecommendations?\b",
    r"\b(?:outside|beyond)\s+(?:of\s+)?what\s+I\s+can\s+help\b",
    r"\bnot\s+something\s+I\s+can\s+help\b",
    r"\bI\s+don(?:'|’)?t\s+have\s+(?:information|details)\s+(?:about|on)\s+specific\s+products\b",
)

_KINDS = r"(?:Savings|Deposit|Account|Card|Loan)s?"
OTHER_PRODUCT_PATTERNS = (
    rf"\bShinhan\s+(?:[A-Z][\w.&'’-]*\s+){{1,4}}{_KINDS}\b",
    r"\b(?:KB|Kookmin|Woori|Hana|NH|Nonghyup|IBK|Kakao\s?Bank|Kakao|Toss\s?Bank|Toss|K\s?bank)\b"
    r"[^.\n]{0,40}?\b(?:Savings|Deposit|Account)s?\b",
    r"[\"“]([^\"”\n]{2,60}?\b(?:Savings|Deposit|Account)\b[^\"”\n]{0,30})[\"”]",
    r"\*\*([^*\n]{2,60}?\b(?:Savings|Deposit|Account)\b[^*\n]{0,30})\*\*",
)

RATE_WORD = r"\binterest\b|\bAPR\b|\bpreferential\s+rates?\b|\byields?\b|금리|이자|우대"
_RATE_CONTEXT = r"\brates?\b|\binterest\b|\bAPR\b|\byields?\b|금리|이자"
_RATE_NUMBER = r"\d\s?%|\bp\.\s?a\.|\bpercent\b"


def _korean_names(name: str) -> tuple[str, ...]:
    return (name, name.removeprefix("신한 ")) if name.startswith("신한 ") else (name,)


def catalog_names(answer: str) -> list[str]:
    found = []
    for name, aliases in CATALOG_ALIASES.items():
        korean = any(k in answer for k in _korean_names(name))
        if korean or any(re.search(p, answer, _I) for p in aliases):
            found.append(name)
    return found


def other_product_candidates(answer: str) -> list[str]:
    """조건표 별칭에 걸리지 않는 상품명 후보(사람 확인)."""
    found: list[str] = []
    for pattern in OTHER_PRODUCT_PATTERNS:
        for match in re.finditer(pattern, answer, _I if "Shinhan" not in pattern else 0):
            text = (match.group(1) if match.groups() else match.group(0)).strip()
            aliased = any(re.search(p, text, _I) for a in CATALOG_ALIASES.values() for p in a)
            if not aliased and text not in found:
                found.append(text)
    return found


def _sentences(answer: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", answer) if s.strip()]


def rate_value(answer: str) -> list[str]:
    """금리 값 언급(위반): %·p.a.와 금리 단어가 같은 문장."""
    return [
        s.strip()
        for s in _sentences(answer)
        if re.search(_RATE_NUMBER, s, _I) and re.search(_RATE_CONTEXT, s, _I)
    ]


def rate_words(answer: str) -> list[str]:
    return sorted({m.group(0).lower() for m in re.finditer(RATE_WORD, answer, _I)})


def declined(answer: str) -> bool:
    return any(re.search(p, answer, _I) for p in DECLINE_PATTERNS)


def product_check(answer: str, tools_called: Sequence[str], out_of_scope: bool) -> dict[str, Any]:
    names = catalog_names(answer)
    candidates = other_product_candidates(answer)
    is_decline = declined(answer)
    behavior = "named_products" if names else ("declined" if is_decline else "other")
    reasons = []
    if behavior == "other":
        reasons.append("거절·상품명 없음")
    if candidates:
        reasons.append("조건표 밖 상품명 후보")
    if is_decline and names:
        reasons.append("거절 문구와 상품명이 함께 나옴")
    return {
        "behavior": behavior,
        "catalogNames": names,
        "otherProductCandidates": candidates,
        "rateValue": rate_value(answer),
        "rateWord": rate_words(answer),
        "wrongCall": out_of_scope and PRODUCT_TOOL in tools_called,
        "needsReview": bool(reasons),
        "reviewReason": "; ".join(reasons),
    }


def summary_lines(rows: Sequence[dict[str, Any]]) -> list[str]:
    """요약 md의 '상품 질문 채점' 절(행에 productCheck가 있을 때만)."""
    checked = [r for r in rows if r.get("productCheck")]
    if not checked:
        return []
    product = [r for r in checked if (r.get("expectedOutcome") or {}).get("type") != "out_of_scope"]
    scope = [r for r in checked if (r.get("expectedOutcome") or {}).get("type") == "out_of_scope"]

    def count(group: list[dict[str, Any]], behavior: str) -> int:
        return sum(1 for r in group if r["productCheck"]["behavior"] == behavior)

    def ref(r: dict[str, Any]) -> str:
        return f"{r['questionId']}#{r['repeat']}"

    def split(group: list[dict[str, Any]]) -> str:
        return (
            f"거절 {count(group, 'declined')} / 상품명 언급 {count(group, 'named_products')}"
            f" / 기타 {count(group, 'other')} (분모 {len(group)})"
        )

    wrong_calls = sum(1 for r in scope if r["productCheck"]["wrongCall"])
    rate_values = sum(1 for r in checked if r["productCheck"]["rateValue"])
    rate_words_n = sum(1 for r in checked if r["productCheck"]["rateWord"])
    out = [
        "",
        "### 상품 질문 채점",
        "",
        f"- 상품 문항: {split(product)}",
        f"- 범위 밖 문항: {split(scope)}, 부르지 말아야 할 때 호출 {wrong_calls}",
        f"- 금리 값 언급(rateValue, 위반): {rate_values}"
        f" / 금리 단어(rateWord, 참고): {rate_words_n}",
    ]
    tools: dict[str, int] = {}
    for r in checked:
        for name in sorted({t.get("toolName") or "-" for t in r.get("tools", [])}) or ["(없음)"]:
            tools[name] = tools.get(name, 0) + 1
    out.append(
        "- 도구 호출 분포(회차 수): " + ", ".join(f"{k} {v}" for k, v in sorted(tools.items()))
    )
    named = [r for r in checked if r["productCheck"]["catalogNames"]]
    others = [r for r in checked if r["productCheck"]["otherProductCandidates"]]
    if named or others:
        out += ["", "| 문항 | 조건표 상품명 | 조건표 밖 후보 |", "|---|---|---|"]
        for r in sorted({id(r): r for r in named + others}.values(), key=ref):
            check = r["productCheck"]
            out.append(
                f"| {ref(r)} | {', '.join(check['catalogNames']) or '-'} | "
                f"{', '.join(check['otherProductCandidates']) or '-'} |"
            )
    rated = [r for r in checked if r["productCheck"]["rateValue"]]
    if rated:
        out += ["", "| 문항 | 금리 값 언급 구절 |", "|---|---|"]
        out += [f"| {ref(r)} | {' / '.join(r['productCheck']['rateValue'])} |" for r in rated]
    review = [r for r in checked if r["productCheck"]["needsReview"]]
    if review:
        out.append(
            "- 사람 확인: "
            + ", ".join(f"{ref(r)}({r['productCheck']['reviewReason']})" for r in review)
        )
    return out

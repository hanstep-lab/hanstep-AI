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

import json
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
    # 측정 뒤 추가(2026-11-16 기준선 사람 확인, 결과 문서 7절): 축약형·나열형·수동형 거절.
    r"\baren(?:'|’)?t\s+(?:yet\s+)?available\b",
    r"\b(?:can(?:'|’)?t|cannot)\s+look\s+up\s+or\s+recommend\b",
    r"\b(?:can(?:'|’)?t|cannot)\s+be\s+recommended\b",
    # 측정 뒤 추가(2026-11-17 v4 1차 사람 확인, 결과 문서 7절): "Chat can't check loans yet".
    r"\b(?:can(?:'|’)?t|cannot)\s+check\s+(?:loans?|(?:credit\s+)?cards?|checking\s+accounts?)\b",
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


ARG_FIELDS = (
    "productType",
    "maxTermMonths",
    "monthlyAmountKrw",
    "accrual",
    "requireForeignerExplicit",
)


def _args(conditions: dict[str, Any] | None) -> dict[str, Any]:
    """도구 인자 5개(없으면 None, 외국인 명시는 False)."""
    data = conditions or {}
    out = {name: data.get(name) for name in ARG_FIELDS}
    out["requireForeignerExplicit"] = bool(data.get("requireForeignerExplicit"))
    return out


def expected_gold(
    expected_args: dict[str, Any], profile: dict[str, Any], as_of: str
) -> tuple[list[str], str | None]:
    """기대 인자·프로필로 추천기를 돌린 통과 집합과 기권 이유(hold-out처럼 gold가 없을 때)."""
    from datetime import date

    from app.chat.product_tools import ACCRUAL_TO_CATALOG, load_catalog
    from app.products import recommender as rec

    catalog = load_catalog()
    args = _args(expected_args)
    args["accrual"] = ACCRUAL_TO_CATALOG.get(args["accrual"] or "")
    result = rec.recommend_filtered(
        [p["productId"] for p in catalog],
        {p["productId"]: p for p in catalog},
        profile,
        args,
        date.fromisoformat(as_of),
        explicit_first=True,
        with_reason=True,
        k=len(catalog),
    )
    return sorted(result["recommended"]), result["reason"]


def tool_check(
    tool: dict[str, Any] | None,
    *,
    expected_args: dict[str, Any] | None,
    expected_outcome: dict[str, Any],
    gold: Sequence[str],
    answer_names: Sequence[str],
) -> dict[str, Any]:
    """v4 채점(측정 전 고정 지표, 결과 문서 3절): 인자, 끝까지(기대 인자 기준), 도구에 없는 상품명.

    tool은 agent.metrics의 상품 도구 측정 값(첫 호출). 위반 상품 = 추천했지만 기대 인자 기준
    통과 집합(gold)에 없는 상품.
    """
    tool_names = [p.get("name") for p in ((tool or {}).get("content") or {}).get("products") or []]
    names_not_in_tool = [n for n in answer_names if n not in tool_names]
    if tool is None:
        return {
            "called": False,
            "argsMatch": None,
            "argsAllMatch": False if expected_args is not None else None,
            "violating": [],
            "recallAt5": None,
            "outcomeCorrect": expected_outcome.get("type") == "out_of_scope",
            "namesNotInTool": names_not_in_tool,
        }
    recommended = list(tool.get("recommended") or [])
    reason = tool.get("abstainReason")
    match = None
    if expected_args is not None:
        got, want = _args(tool.get("conditions")), _args(expected_args)
        match = {name: got[name] == want[name] for name in ARG_FIELDS}
    gold_set = set(gold)
    kind = expected_outcome.get("type")
    if kind == "recommend":
        correct = bool(recommended)
    elif kind == "abstain":
        correct = not recommended and reason == expected_outcome.get("reason")
    else:  # out_of_scope: 상품 도구를 부르면 틀림
        correct = False
    recall = (
        len(set(recommended[:5]) & gold_set) / min(len(gold_set), 5)
        if kind == "recommend" and gold_set
        else None
    )
    return {
        "called": True,
        "argsMatch": match,
        "argsAllMatch": None if match is None else all(match.values()),
        "violating": [pid for pid in recommended if pid not in gold_set],
        "recallAt5": recall,
        "abstainReason": reason,
        "outcomeCorrect": correct,
        "namesNotInTool": names_not_in_tool,
    }


def tool_summary_lines(rows: Sequence[dict[str, Any]]) -> list[str]:
    """v4 상품 도구 채점 요약(행에 productToolCheck가 있을 때만)."""
    checked = [r for r in rows if r.get("productToolCheck")]
    if not checked:
        return []

    def ref(r: dict[str, Any]) -> str:
        return f"{r['questionId']}#{r['repeat']}"

    with_args = [r for r in checked if r.get("expectedArgs") is not None]
    called = [r for r in with_args if r["productToolCheck"]["called"]]
    all_match = sum(1 for r in with_args if r["productToolCheck"]["argsAllMatch"])
    # 순서 방식(search = B, catalog* = A). 측정 조건 확인용(채점에는 쓰지 않음).
    ranked: dict[str, int] = {}
    for r in checked:
        how = (r.get("productTool") or {}).get("rankedBy")
        if how:
            ranked[how] = ranked.get(how, 0) + 1
    out = [
        "",
        "### 상품 도구 채점(v4)",
        "",
        "- 순서 방식(도구 호출 회차): "
        + (", ".join(f"{k} {v}" for k, v in sorted(ranked.items())) or "-"),
        f"- 상품 문항에서 도구 호출: {len(called)}/{len(with_args)}",
        f"- 인자 전체 일치: {all_match}/{len(with_args)}"
        + (f" ({all_match / len(with_args):.0%})" if with_args else ""),
    ]
    if called:
        per_field = ", ".join(
            f"{name} {sum(1 for r in called if r['productToolCheck']['argsMatch'][name])}"
            f"/{len(called)}"
            for name in ARG_FIELDS
        )
        out.append(f"- 필드별 일치(호출한 회차): {per_field}")
    violating = [r for r in checked if r["productToolCheck"]["violating"]]
    n_violating = sum(len(r["productToolCheck"]["violating"]) for r in checked)
    out.append(
        f"- 위반 상품(기대 인자 기준): {n_violating}"
        + (" — " + ", ".join(ref(r) for r in violating) if violating else "")
    )
    recalls = [
        r["productToolCheck"]["recallAt5"]
        for r in checked
        if r["productToolCheck"].get("recallAt5") is not None
    ]
    if recalls:
        out.append(f"- gold Recall@5 평균(추천 문항): {sum(recalls) / len(recalls):.3f}")
    for kind, label in (("abstain", "기권 정확도·이유 코드"), ("recommend", "추천 문항 추천함")):
        group = [r for r in checked if (r.get("expectedOutcome") or {}).get("type") == kind]
        if group:
            ok = sum(1 for r in group if r["productToolCheck"]["outcomeCorrect"])
            out.append(f"- {label}: {ok}/{len(group)}")
    scope = [r for r in checked if (r.get("expectedOutcome") or {}).get("type") == "out_of_scope"]
    if scope:
        wrong = [r for r in scope if r["productToolCheck"]["called"]]
        out.append(
            f"- 부르지 말아야 할 때 호출: {len(wrong)}/{len(scope)}"
            + (" — " + ", ".join(ref(r) for r in wrong) if wrong else "")
        )
    names = [r for r in checked if r["productToolCheck"]["namesNotInTool"]]
    out.append(
        f"- 도구에 없는 상품명: {len(names)}"
        + (
            " — "
            + "; ".join(
                f"{ref(r)}: {', '.join(r['productToolCheck']['namesNotInTool'])}" for r in names
            )
            if names
            else ""
        )
    )
    mismatched = [r for r in called if not r["productToolCheck"]["argsAllMatch"]]
    if mismatched:
        out += ["", "| 문항 | 기대 인자 | 도구 인자 |", "|---|---|---|"]
        for r in mismatched:
            got = (r.get("productTool") or {}).get("conditions")
            out.append(
                f"| {ref(r)} | {json.dumps(r['expectedArgs'], ensure_ascii=False)} | "
                f"{json.dumps(got, ensure_ascii=False)} |"
            )
    return out


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

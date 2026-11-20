"""프롬프트 v4.5 측정(#114) 채점: 입력값 기준 숫자·출처 언급, 고른 목표 언급, 상품 질문(측정 전용).

기준: specs/chat/chat_v45_measure_v1.md 3절. 서버는 쓰지 않는다.

- 기대값은 문항에 적지 않고 측정 때 읽은 INT-06 요약(행의 toolResult)에서 정한다.
  - 입력값: declaredCommitments의 fixedLiving·remittance·variableExpense. 입력이 없거나 0인 항목은
    거래 기준(analysis) 값을 쓴다(BE #151 MonthlyBalance와 같은 규칙).
  - 입력값 기준 여유자금 = analysis.avgMonthlyIncome − 위 세 항목.
    채점용 계산이라 LLM에 준 값이 아닐 수 있다.
  - 고른 목표: goals[].goalType 순서(INT-06은 ACTIVE 목표를 id 순서로 준다).
- 숫자 일치: 답변 숫자(number_check.scan_numbers)에 기대값이 있으면 일치.
  1,000·10,000원 반올림 값도 일치로 본다.
  여유자금이 음수면 절댓값으로 본다("short by 600,000 won").
- 거래 기준 숫자 사용: 답변에 거래 기준 값(입력값과 다른 항목만)이 있으면 true.
- 출처 언급: "you entered", "based on the amounts you entered" 같은 표현(SOURCE_PATTERN).
- 목표 언급: 목표 종류별 낱말(GOAL_PATTERNS). 비율 = 언급한 고른 목표 수 ÷ 고른 목표 수.
  순서 일치 = 고른 목표를 모두 언급했고 첫 언급 위치가 고른 순서와 같음(하나라도 빠지면 null).
- 상품 질문: 상품 도구가 쓴 여유자금·추천 수·기권 이유(행의 productTool), 카드(blocks의
  PRODUCT_RECOMMEND_CARD)와 도구 결과 일치, 금리 숫자 직접 언급(답변의 % 숫자), 카드 가리킴("card").
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from app.chat.number_check import rounded_values, scan_numbers

ITEMS = ("fixedLiving", "remittance", "variableExpense")
SOURCE_PATTERN = re.compile(
    r"\byou(?:'ve| have)? (?:entered|put in|provided|set|told|gave|reported|input)\b"
    r"|\bbased on (?:what|the (?:amounts?|figures?|numbers?|values?)) you\b"
    r"|\byour (?:own )?(?:input|inputs|entries|entered (?:amounts?|values?|figures?))\b"
    r"|입력하신",
    re.IGNORECASE,
)
GOAL_PATTERNS = {
    "LUMP_SUM": re.compile(
        r"lump[- ]sum|sav(?:e|ing) up|savings goal|target amount|6,000,000|6 million", re.I
    ),
    "CREDIT": re.compile(r"\bcredit\b", re.I),
    "SETTLEMENT": re.compile(
        r"E-7-4|long[- ]term (?:stay|settle\w*)|\bsettl\w+|visa roadmap|permanent resid", re.I
    ),
    "LIVING_COST": re.compile(
        r"living (?:costs?|expenses?)|variable (?:spending|expenses?)|\bbudget\w*|daily spending",
        re.I,
    ),
    "REMITTANCE": re.compile(r"\bremit\w*|send(?:ing)? money (?:home|abroad)", re.I),
}


def _found(answer: str) -> set[Decimal]:
    return {abs(v) for _, v, kind, _ in scan_numbers(answer) if v is not None and kind != "date"}


def _matches(value: int | None, found: set[Decimal]) -> bool:
    if value is None:
        return False
    target = abs(Decimal(value))
    return target in found or bool(rounded_values({target}) & found)


def expected_values(tool_result: Mapping[str, Any]) -> dict[str, Any]:
    """입력값(없으면 거래 기준) 세 항목과 입력값 기준 여유자금, 거래 기준 값."""
    analysis = tool_result.get("analysis") or {}
    declared = tool_result.get("declaredCommitments") or {}
    transactions = {
        "fixedLiving": analysis.get("fixedLiving"),
        "remittance": analysis.get("remittance"),
        "variableExpense": (analysis.get("variableExpense") or {}).get("avg"),
        "spare": analysis.get("spare"),
    }
    inputs: dict[str, int | None] = {}
    sources: dict[str, str] = {}
    for item in ITEMS:
        value = declared.get(item)
        if value:
            inputs[item], sources[item] = value, "USER_INPUT"
        else:
            inputs[item], sources[item] = transactions[item], "TRANSACTIONS"
    income = analysis.get("avgMonthlyIncome")
    parts = [inputs[i] for i in ITEMS]
    spare = None if income is None or None in parts else income - sum(p or 0 for p in parts)
    return {
        "input": {**inputs, "spare": spare},
        "sources": sources,
        "transactions": transactions,
    }


def input_check(answer: str, expect: str, tool_result: Mapping[str, Any]) -> dict[str, Any]:
    """입력값 질문: 기대 숫자 일치, 거래 기준 숫자 사용, 출처 언급."""
    values = expected_values(tool_result)
    found = _found(answer)
    wanted = ("spare",) if expect == "spare" else ITEMS
    matched = {name: _matches(values["input"][name], found) for name in wanted}
    # 입력값과 다른 거래 기준 값만 센다(같은 값이면 어느 쪽인지 알 수 없다).
    transaction_hits = {
        name: _matches(values["transactions"][name], found)
        for name in (*ITEMS, "spare")
        if values["transactions"][name] is not None
        and values["transactions"][name] != values["input"][name]
    }
    return {
        "kind": "input",
        "expect": expect,
        "expected": {name: values["input"][name] for name in wanted},
        "matched": matched,
        "inputMatch": bool(matched) and all(matched.values()),
        "usedTransactionNumbers": any(transaction_hits.values()),
        "transactionHits": [name for name, hit in transaction_hits.items() if hit],
        "sourceMentioned": bool(SOURCE_PATTERN.search(answer)),
    }


def goal_check(answer: str, tool_result: Mapping[str, Any]) -> dict[str, Any]:
    """열린 질문: 고른 목표 언급 비율과 언급 순서."""
    goals = [g.get("goalType") for g in tool_result.get("goals") or [] if g.get("goalType")]
    positions: dict[str, int] = {}
    for goal in goals:
        pattern = GOAL_PATTERNS.get(goal)
        hit = pattern.search(answer) if pattern else None
        if hit:
            positions[goal] = hit.start()
    mentioned = [g for g in goals if g in positions]
    order_ok: bool | None = None
    if goals and len(mentioned) == len(goals):
        order_ok = sorted(goals, key=lambda g: positions[g]) == goals
    return {
        "kind": "goals",
        "goals": goals,
        "mentioned": mentioned,
        "ratio": round(len(mentioned) / len(goals), 3) if goals else None,
        "orderOk": order_ok,
    }


PRODUCT_CARD = "PRODUCT_RECOMMEND_CARD"
CARD_POINTER = re.compile(r"\bcards?\b", re.IGNORECASE)


def product_check(row: Mapping[str, Any], answer: str) -> dict[str, Any]:
    """상품 질문(V06): 도구가 쓴 여유자금·추천 결과, 카드, 금리 숫자 직접 언급, 카드 가리킴."""
    content = (row.get("productTool") or {}).get("content") or {}
    products = content.get("products") or []
    cards = [b for b in row.get("blocks") or [] if b.get("type") == PRODUCT_CARD]
    card = cards[-1] if cards else None
    rate_numbers = [text for text, _, kind, _ in scan_numbers(answer) if kind == "percent"]
    return {
        "kind": "product",
        "monthlySpareKrw": (content.get("user") or {}).get("monthlySpareKrw"),
        "productCount": len(products) if content else None,
        "abstainReason": content.get("abstainReason"),
        "cardCount": len(cards),
        # 카드 값이 도구 결과와 같은지: 상품 수·기권 이유(카드가 없으면 None).
        "cardMatchesTool": (
            None
            if card is None
            else len(card.get("products") or []) == len(products)
            and card.get("abstainReason") == content.get("abstainReason")
        ),
        "rateNumbers": rate_numbers,
        "statesRateNumbers": bool(rate_numbers),
        "pointsToCard": bool(CARD_POINTER.search(answer)),
    }


def check(row: Mapping[str, Any], answer: str) -> dict[str, Any] | None:
    kind = row.get("v45Check")
    tool_result = row.get("toolResult") or {}
    if kind == "input":
        return input_check(answer, str(row.get("v45Expect") or "items"), tool_result)
    if kind == "goals":
        return goal_check(answer, tool_result)
    if kind == "product":
        return product_check(row, answer)
    return None


def _ratio(values: Sequence[bool]) -> str:
    return f"{sum(values)}/{len(values)}" if values else "-"


def summary_lines(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """한 설정의 v4.5 지표 요약(완료 회차만)."""
    done = [r for r in rows if r.get("v45Result") and r.get("status") == "done"]
    if not done:
        return []
    out = ["", "### v4.5 지표(#114)", ""]
    inputs = [r for r in done if r["v45Result"]["kind"] == "input"]
    if inputs:
        out += [
            "| 문항 | 회차 | 입력값 숫자 일치 | 거래 기준 숫자 사용 | 출처 언급 "
            "| 근거 없는 숫자 없음 |",
            "|---|---|---|---|---|---|",
        ]
        for qid in dict.fromkeys(r["questionId"] for r in inputs):
            mine = [r for r in inputs if r["questionId"] == qid]
            res = [r["v45Result"] for r in mine]
            out.append(
                f"| {qid} | {len(mine)} | {_ratio([x['inputMatch'] for x in res])} | "
                f"{_ratio([x['usedTransactionNumbers'] for x in res])} | "
                f"{_ratio([x['sourceMentioned'] for x in res])} | "
                f"{_ratio([not (r.get('numberCheck') or {}).get('unsupported') for r in mine])} |"
            )
        expected = inputs[0]["v45Result"]["expected"]
        out.append(f"- 기대값(입력값 기준, 첫 행): {expected}")
    goals = [r for r in done if r["v45Result"]["kind"] == "goals"]
    if goals:
        out += [
            "",
            "| 문항 | 회차 | 고른 목표 | 언급 비율 평균 | 모두 언급 | 순서 일치 "
            "| 근거 없는 숫자 없음 |",
            "|---|---|---|---|---|---|---|",
        ]
        for qid in dict.fromkeys(r["questionId"] for r in goals):
            mine = [r for r in goals if r["questionId"] == qid]
            res = [r["v45Result"] for r in mine]
            ratios = [x["ratio"] for x in res if x["ratio"] is not None]
            avg = f"{sum(ratios) / len(ratios):.2f}" if ratios else "-"
            out.append(
                f"| {qid} | {len(mine)} | {'·'.join(res[0]['goals']) or '-'} | {avg} | "
                f"{_ratio([x['ratio'] == 1 for x in res])} | "
                f"{_ratio([x['orderOk'] is True for x in res])} | "
                f"{_ratio([not (r.get('numberCheck') or {}).get('unsupported') for r in mine])} |"
            )
    products = [r for r in done if r["v45Result"]["kind"] == "product"]
    if products:
        out += [
            "",
            "| 문항#회 | 도구 여유자금 | 추천 수 | 기권 이유 | 카드 | 카드=도구 | 금리 숫자 직접 "
            "| 카드 가리킴 | 근거 없는 숫자 |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for r in products:
            x = r["v45Result"]
            unsupported = ", ".join((r.get("numberCheck") or {}).get("unsupported") or []) or "-"
            out.append(
                f"| {r['questionId']}#{r.get('repeat')} | {x['monthlySpareKrw']} | "
                f"{x['productCount']} | {x['abstainReason'] or '-'} | {x['cardCount']} | "
                f"{'-' if x['cardMatchesTool'] is None else x['cardMatchesTool']} | "
                f"{', '.join(x['rateNumbers']) or '-'} | {x['pointsToCard']} | {unsupported} |"
            )
    return out

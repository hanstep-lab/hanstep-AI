"""Versioned chat system prompts (CHAT_PROMPT_VERSION picks one;
the default is Settings.chat_prompt_version).
"""

from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).resolve().parent
# Setting value -> version reported in agent.done / agent.metrics.
PROMPT_VERSIONS = {
    "v1": "chat-v1",
    "v1.1": "chat-v1.1",
    "v2": "chat-v2",
    "v3": "chat-v3",
    "v3.1": "chat-v3.1",
    "v3.2": "chat-v3.2",
    "v4": "chat-v4",
    "v4.1": "chat-v4.1",
    "v4.2": "chat-v4.2",
    "v4.3a": "chat-v4.3a",
    "v5": "chat-v5",
}
PROMPT_VERSION = PROMPT_VERSIONS["v1"]  # the default

# v2를 만들 때 v1.1에서 바꾸는 줄: 비자 도구가 생겼으니 "비자 점수는 아직 안 됨"만 뺀다.
VISA_UNAVAILABLE_LINE = (
    "- Visa scores and financial product recommendations are not available in chat yet. "
    "Say so briefly."
)
PRODUCTS_UNAVAILABLE_LINE = (
    "- Financial product recommendations are not available in chat yet. Say so briefly."
)
# v3를 만들 때 v2에서 바꾸는 줄: 송금 조회 도구가 생겼으니 "송금 메뉴로 안내"를 송금 규칙으로.
REMITTANCE_MENU_LINE = (
    "- Never send money. For remittances, guide the user to the overseas remittance menu."
)
REMITTANCE_RULES_LINE = "- Never send money. For remittances, follow the remittance rules below."
# v3.1·v3.2: v3 송금 규칙 끝에 환율 규칙 한 줄을 더한다(이슈 #45 환율 단위 표기 비교안).
RATE_TEXT_RULE = (
    "- When you mention the exchange rate, copy appliedRate.rateText exactly. "
    "Do not restate it in other units or directions."
)
NO_RATE_RULE = (
    "- Do not state the exchange rate in remittance replies. The confirmation card shows it. "
    "Say only who receives how much."
)


def _replace_line(base: str, old: str, new: str, what: str) -> list[str]:
    """base에서 old 한 줄을 new로 바꾼다. old가 정확히 한 번 있지 않으면 시작할 때 실패한다."""
    lines = base.splitlines()
    if lines.count(old) != 1:
        raise ValueError(f"the base prompt must contain the {what} line exactly once")
    lines[lines.index(old)] = new
    return lines


def _with_visa_rules(base: str, visa_rules: str) -> str:
    """v1.1 + 비자 규칙(visa_rules.md). 바꿀 줄이 v1.1에 없으면 시작할 때 실패한다."""
    lines = _replace_line(
        base, VISA_UNAVAILABLE_LINE, PRODUCTS_UNAVAILABLE_LINE, "visa-unavailable"
    )
    return "\n".join(lines) + "\n\n" + visa_rules


def _with_remittance_rules(base: str, remittance_rules: str) -> str:
    """v2 + 송금 규칙(remittance_rules.md). 바꿀 줄이 v2에 없으면 시작할 때 실패한다."""
    lines = _replace_line(base, REMITTANCE_MENU_LINE, REMITTANCE_RULES_LINE, "remittance-menu")
    return "\n".join(lines).rstrip("\n") + "\n\n" + remittance_rules


# v4를 만들 때 v3에서 바꾸는 줄: 상품 추천 도구(#65)가 생겼으니
# '상품 추천은 아직 안 됨'을 상품 규칙으로.
PRODUCTS_RULES_LINE = (
    "- Recommend only savings and time deposit products, following the product rules below. "
    "Other product recommendations (cards, loans, checking accounts) are not available in chat "
    "yet. Say so briefly."
)


def _with_product_rules(base: str, product_rules: str) -> str:
    """v3 + 상품 규칙(product_rules.md). 바꿀 줄이 v3에 없으면 시작할 때 실패한다."""
    lines = _replace_line(base, PRODUCTS_UNAVAILABLE_LINE, PRODUCTS_RULES_LINE, "products")
    return "\n".join(lines).rstrip("\n") + "\n\n" + product_rules


# v4.1(2026-11-17, v4 1차 측정 결과를 보고 수정):
# STAY_TOO_SHORT 안내는 남은 체류 기간과 상품 기간만.
V4_STAY_RULE = (
    "STAY_TOO_SHORT: the products that fit last longer than the user's remaining stay; give the "
    "remaining months and do not suggest extending the stay."
)
V4_1_STAY_RULE = (
    "STAY_TOO_SHORT: say only the user's remaining months of stay and that the fitting products "
    "last longer; do not suggest extending the stay or the visa."
)


def _with_stay_rule(base: str) -> str:
    """v4에서 STAY_TOO_SHORT 안내 문장 하나만 바꾼다. 바꿀 문장이 정확히 한 번 없으면 실패한다."""
    if base.count(V4_STAY_RULE) != 1:
        raise ValueError("the base prompt must contain the STAY_TOO_SHORT rule exactly once")
    return base.replace(V4_STAY_RULE, V4_1_STAY_RULE)


def _with_rule(base: str, rule: str) -> str:
    """base(v3) 끝에 규칙 한 줄을 더한다. v3의 마지막 절은 송금 규칙이다."""
    return base.rstrip("\n") + "\n" + rule + "\n"


_V1_1 = (_DIR / "system_v1_1.md").read_text(encoding="utf-8")
_V2 = _with_visa_rules(_V1_1, (_DIR / "visa_rules.md").read_text(encoding="utf-8"))
_V3 = _with_remittance_rules(_V2, (_DIR / "remittance_rules.md").read_text(encoding="utf-8"))
_SYSTEM = {
    "v1": (_DIR / "system_v1.md").read_text(encoding="utf-8"),
    # Same rules as v1 except the numbers rule and the judgments rule.
    "v1.1": _V1_1,
    # v1.1 그대로 + 비자 도구 규칙. v1.1 숫자·판단 규칙을 고치면 v2에도 그대로 반영된다.
    "v2": _V2,
    # v2 그대로 + 송금 규칙(되묻기·견적·확인 카드). 송금 조회 도구는 v3에서만 붙는다.
    "v3": _V3,
    # v3 그대로 + 환율 규칙 한 줄(송금 규칙 끝). 송금 도구 구성은 v3와 같다.
    "v3.1": _with_rule(_V3, RATE_TEXT_RULE),  # rateText를 그대로 옮겨 쓰기
    "v3.2": _with_rule(_V3, NO_RATE_RULE),  # 답변에 환율을 쓰지 않기
    # v3 그대로 + 상품 추천 규칙(#65). 상품 추천 도구는 v4에서만 붙는다.
    "v4": _with_product_rules(_V3, (_DIR / "product_rules.md").read_text(encoding="utf-8")),
}
# v4 그대로 + STAY_TOO_SHORT 안내 한 문장(체류·비자 연장을 권하지 않음). 도구 설명도 v4.1용을 쓴다.
_SYSTEM["v4.1"] = _with_stay_rule(_SYSTEM["v4"])
# v4.1 그대로 + 금융 정책 규칙(#88). 금융 정책 검색 도구는 v4.2에서만 붙는다.
# v4.1 문구는 바꾸지 않는다.
_SYSTEM["v4.2"] = (
    _SYSTEM["v4.1"].rstrip("\n")
    + "\n\n"
    + (_DIR / "finance_policy_rules.md").read_text(encoding="utf-8")
)
# v4.3a(#88 스펙 가드 ①): v4.2 그대로 + 규정 규칙 끝에 한 줄. after v4.2의 위험 오답 7이 모두
# 제4-4조 ④ '수령'(받는 돈)을 송금으로 옮긴 것이라, 수령·지급 방향과 적용 대상을 확인하게 한다.
DIRECTION_RULE = (
    "- 수령 means receiving money and 지급 means paying or sending money. Before you cite a rule, "
    "check that its direction (receiving or sending) and the people it applies to match the "
    "user's question; if either does not match, do not use that rule for the answer."
)
_SYSTEM["v4.3a"] = _with_rule(_SYSTEM["v4.2"], DIRECTION_RULE)


def _with_fx_rules(base: str, fx_rules: str) -> str:
    """v4.1 + 환율 진단 규칙(fx_rules.md, #91). v4.1의 줄은 바꾸지 않고 끝에 절을 더한다."""
    return base.rstrip("\n") + "\n\n" + fx_rules


# v4.1 그대로 + 환율 진단 규칙(#91). 환율 진단 도구 fx_rate_summary는 v5에서만 붙는다.
_SYSTEM["v5"] = _with_fx_rules(_SYSTEM["v4.1"], (_DIR / "fx_rules.md").read_text(encoding="utf-8"))


def system_prompt(language: str, version: str = "v1") -> str:
    return _SYSTEM[version].replace("{language}", language)

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
}


def system_prompt(language: str, version: str = "v1") -> str:
    return _SYSTEM[version].replace("{language}", language)

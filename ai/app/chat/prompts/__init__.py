"""Versioned chat system prompts (CHAT_PROMPT_VERSION picks one; v1 is the default)."""

from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).resolve().parent
# Setting value -> version reported in agent.done / agent.metrics.
PROMPT_VERSIONS = {"v1": "chat-v1", "v1.1": "chat-v1.1", "v2": "chat-v2"}
PROMPT_VERSION = PROMPT_VERSIONS["v1"]  # the default

# v2를 만들 때 v1.1에서 바꾸는 줄: 비자 도구가 생겼으니 "비자 점수는 아직 안 됨"만 뺀다.
VISA_UNAVAILABLE_LINE = (
    "- Visa scores and financial product recommendations are not available in chat yet. "
    "Say so briefly."
)
PRODUCTS_UNAVAILABLE_LINE = (
    "- Financial product recommendations are not available in chat yet. Say so briefly."
)


def _with_visa_rules(base: str, visa_rules: str) -> str:
    """v1.1 + 비자 규칙(visa_rules.md). 바꿀 줄이 v1.1에 없으면 시작할 때 실패한다."""
    lines = base.splitlines()
    if lines.count(VISA_UNAVAILABLE_LINE) != 1:
        raise ValueError("system_v1_1.md must contain the visa-unavailable line exactly once")
    lines[lines.index(VISA_UNAVAILABLE_LINE)] = PRODUCTS_UNAVAILABLE_LINE
    return "\n".join(lines) + "\n\n" + visa_rules


_V1_1 = (_DIR / "system_v1_1.md").read_text(encoding="utf-8")
_SYSTEM = {
    "v1": (_DIR / "system_v1.md").read_text(encoding="utf-8"),
    # Same rules as v1 except the numbers rule and the judgments rule.
    "v1.1": _V1_1,
    # v1.1 그대로 + 비자 도구 규칙. v1.1 숫자·판단 규칙을 고치면 v2에도 그대로 반영된다.
    "v2": _with_visa_rules(_V1_1, (_DIR / "visa_rules.md").read_text(encoding="utf-8")),
}


def system_prompt(language: str, version: str = "v1") -> str:
    return _SYSTEM[version].replace("{language}", language)

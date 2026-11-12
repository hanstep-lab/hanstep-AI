"""Versioned chat system prompts (CHAT_PROMPT_VERSION picks one; v1 is the default)."""

from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).resolve().parent
# Setting value -> version reported in agent.done / agent.metrics.
PROMPT_VERSIONS = {"v1": "chat-v1", "v1.1": "chat-v1.1", "v2": "chat-v2"}
PROMPT_VERSION = PROMPT_VERSIONS["v1"]  # the default
_SYSTEM = {
    "v1": (_DIR / "system_v1.md").read_text(encoding="utf-8"),
    # Same rules as v1 except the numbers rule and the judgments rule.
    "v1.1": (_DIR / "system_v1_1.md").read_text(encoding="utf-8"),
    # v1.1 규칙 + 비자 도구 규칙(v1.1의 "비자 점수는 아직 안 됨" 줄은 뺐다).
    "v2": (_DIR / "system_v2.md").read_text(encoding="utf-8"),
}


def system_prompt(language: str, version: str = "v1") -> str:
    return _SYSTEM[version].replace("{language}", language)

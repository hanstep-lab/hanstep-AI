"""Versioned chat system prompts."""

from __future__ import annotations

from pathlib import Path

# chat-v2: 비자 도구(정책 검색·E-7-4 점수) 규칙 추가. chat-v1(system_v1.md)은 기록으로 남긴다.
PROMPT_VERSION = "chat-v2"
_SYSTEM = (Path(__file__).resolve().parent / "system_v2.md").read_text(encoding="utf-8")


def system_prompt(language: str) -> str:
    return _SYSTEM.replace("{language}", language)

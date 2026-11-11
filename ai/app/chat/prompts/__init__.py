"""Versioned chat system prompts."""

from __future__ import annotations

from pathlib import Path

PROMPT_VERSION = "chat-v1"
_SYSTEM_V1 = (Path(__file__).resolve().parent / "system_v1.md").read_text(encoding="utf-8")


def system_prompt(language: str) -> str:
    return _SYSTEM_V1.replace("{language}", language)

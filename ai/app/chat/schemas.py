"""INT-05 ``POST /internal/v1/agent/chat/stream`` request and per-request LLM settings."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

from app.chat.prompts import PROMPT_VERSIONS
from app.core.config import (
    ChatEffort,
    ChatPromptVersion,
    ChatThinking,
    Settings,
    check_chat_config,
)

MAX_HISTORY = 6


class _CamelRequest(BaseModel):
    # Same convention as the other internal APIs: camelCase only, unknown fields rejected.
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=False, extra="forbid")


class HistoryItem(_CamelRequest):
    role: Literal["USER", "ASSISTANT"]
    content: str = Field(max_length=20000)


class ChatStreamRequest(_CamelRequest):
    run_id: str = Field(min_length=1, max_length=64)
    user_id: int = Field(gt=0)
    session_id: int = Field(gt=0)
    language: Literal["en", "ko", "vi", "zh"]
    # Oldest first, as Spring sends it.
    history: list[HistoryItem] = Field(default_factory=list, max_length=MAX_HISTORY)
    message: str = Field(min_length=1, max_length=2000)


class ConfigOverride(_CamelRequest):
    """``X-Chat-Config`` header (measurement only, honoured when CHAT_ALLOW_CONFIG_OVERRIDE)."""

    model: str | None = Field(default=None, min_length=1, max_length=100)
    thinking: ChatThinking | None = None
    effort: ChatEffort | None = None
    max_tokens: int | None = Field(default=None, ge=1, le=64000)
    judgment_facts: bool | None = None
    prompt_version: ChatPromptVersion | None = None


@dataclass(frozen=True)
class LlmConfig:
    model: str
    thinking: str
    effort: str
    max_tokens: int
    judgment_facts: bool = False
    prompt_version: str = "v1"  # a PROMPT_VERSIONS key

    @classmethod
    def from_settings(cls, settings: Settings) -> LlmConfig:
        return cls(
            model=settings.chat_model_id,
            thinking=settings.chat_thinking,
            effort=settings.chat_effort,
            max_tokens=settings.chat_max_tokens,
            judgment_facts=settings.chat_judgment_facts,
            prompt_version=settings.chat_prompt_version,
        )

    def with_override(self, raw_json: str) -> LlmConfig:
        """Apply an ``X-Chat-Config`` JSON value. ValueError on bad JSON or a bad combination."""
        try:
            override = ConfigOverride.model_validate_json(raw_json)
        except ValidationError as exc:
            raise ValueError("invalid X-Chat-Config") from exc
        changes: dict[str, Any] = {
            k: v for k, v in override.model_dump(exclude_none=True).items() if v is not None
        }
        config = replace(self, **changes)
        check_chat_config(config.model, config.thinking, config.effort)
        return config

    @property
    def prompt_version_id(self) -> str:
        return PROMPT_VERSIONS[self.prompt_version]

    def to_json(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "thinking": self.thinking,
            "effort": self.effort or None,
            "maxTokens": self.max_tokens,
            "promptVersion": self.prompt_version_id,
            "judgmentFacts": self.judgment_facts,
        }

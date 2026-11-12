"""Chat system prompt versions: v1 is the default and unchanged, v1.1 differs in two lines."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.chat.prompts import PROMPT_VERSION, PROMPT_VERSIONS, system_prompt
from app.chat.schemas import ChatStreamRequest, LlmConfig
from app.chat.stream import build_messages
from app.core.config import Settings

pytestmark = pytest.mark.unit

PROMPTS = Path(__file__).resolve().parents[2] / "app" / "chat" / "prompts"
V1_NUMBERS = (
    "- Use only numbers that appear in the tool result. Do not calculate new amounts, ratios or "
    "totals over a period (for example yearly savings or a share of income). If a question needs "
    "such a calculation, say you cannot calculate it for them yet."
)
V11_NUMBERS = (
    "- Use only numbers that appear in the tool result, including any judgment facts. Do not "
    "calculate new numbers yourself. If an exact number is not in the tool result, still give the "
    "conclusion the available numbers support and say the exact amount is not shown."
)
V1_JUDGMENT = "- Do not present anything as an actual approval, a credit score or a guarantee."
V11_JUDGMENT = (
    "- You may give judgments based on past averages, such as whether an amount fits the usual "
    "monthly budget. Give the conclusion first, then the numbers behind it, then a short note that "
    "it is based on past averages. Do not present anything as an actual approval, a credit score "
    "or a guarantee."
)


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def test_v1_unchanged_when_judgment_is_off() -> None:
    # The off setting (judgmentFacts false, v1) still sends the v1 prompt byte for byte.
    assert PROMPT_VERSION == "chat-v1"
    assert PROMPT_VERSIONS == {"v1": "chat-v1", "v1.1": "chat-v1.1"}
    v1_file = (PROMPTS / "system_v1.md").read_text(encoding="utf-8")
    assert system_prompt("en") == v1_file.replace("{language}", "en")
    assert system_prompt("ko") == system_prompt("ko", "v1")
    config = LlmConfig.from_settings(_settings(chat_judgment_facts=False, chat_prompt_version="v1"))
    assert (config.prompt_version, config.prompt_version_id) == ("v1", "chat-v1")
    assert config.judgment_facts is False


def test_default_is_judgment_facts_v1_1_haiku() -> None:
    # Chosen by the judgment facts comparison (2026-11-12): haiku-facts-prompt.
    settings = _settings()
    assert settings.chat_judgment_facts is True
    assert settings.chat_prompt_version == "v1.1"
    assert settings.chat_model == "claude-haiku-5-5"
    config = LlmConfig.from_settings(settings)
    assert config.model == "claude-haiku-5-5"
    assert config.judgment_facts is True
    assert (config.prompt_version, config.prompt_version_id) == ("v1.1", "chat-v1.1")


def test_v1_1_differs_only_in_two_lines() -> None:
    v1 = system_prompt("en", "v1").splitlines()
    v11 = system_prompt("en", "v1.1").splitlines()
    assert len(v1) == len(v11)
    changed = [(a, b) for a, b in zip(v1, v11, strict=True) if a != b]
    assert changed == [(V1_NUMBERS, V11_NUMBERS), (V1_JUDGMENT, V11_JUDGMENT)]


def test_spare_definition_is_not_in_the_prompt() -> None:
    # It lives only in the tool's judgment facts, so the two changes can be measured apart.
    assert "spare =" not in system_prompt("en", "v1.1")
    assert "already subtracted" not in system_prompt("en", "v1.1")


def test_setting_picks_the_system_message() -> None:
    config = LlmConfig.from_settings(_settings(chat_prompt_version="v1.1"))
    assert config.prompt_version_id == "chat-v1.1"
    request = ChatStreamRequest.model_validate(
        {"runId": "run_x", "userId": 1, "sessionId": 1, "language": "vi", "message": "hi"}
    )
    system = build_messages(request, config.prompt_version)[0].content
    assert system == system_prompt("vi", "v1.1")
    assert build_messages(request)[0].content == system_prompt("vi")

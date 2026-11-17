"""Chat prompt versions: v1 unchanged, v1.1 two lines, v2 visa, v3 remittance, v4 product rules."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import get_args

import pytest

from app.api.routes.chat import (
    PRODUCT_PROMPT_VERSIONS,
    REMITTANCE_PROMPT_VERSIONS,
    VISA_PROMPT_VERSIONS,
)
from app.chat.product_tools import RECOMMEND_SAVINGS_PRODUCTS
from app.chat.prompts import (
    PROMPT_VERSION,
    PROMPT_VERSIONS,
    V4_1_STAY_RULE,
    V4_STAY_RULE,
    _with_product_rules,
    _with_remittance_rules,
    _with_stay_rule,
    _with_visa_rules,
    system_prompt,
)
from app.chat.remittance_tools import REMITTANCE_TOOL_NAMES
from app.chat.schemas import ChatStreamRequest, LlmConfig
from app.chat.stream import build_messages
from app.chat.visa_tools import VISA_TOOL_NAMES
from app.core.config import ChatPromptVersion, Settings

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
V11_VISA_UNAVAILABLE = (
    "- Visa scores and financial product recommendations are not available in chat yet. "
    "Say so briefly."
)
V2_PRODUCTS = "- Financial product recommendations are not available in chat yet. Say so briefly."
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
    assert PROMPT_VERSIONS == {
        "v1": "chat-v1",
        "v1.1": "chat-v1.1",
        "v2": "chat-v2",
        "v3": "chat-v3",
        "v3.1": "chat-v3.1",
        "v3.2": "chat-v3.2",
        "v4": "chat-v4",
        "v4.1": "chat-v4.1",
    }
    v1_file = (PROMPTS / "system_v1.md").read_text(encoding="utf-8")
    assert system_prompt("en") == v1_file.replace("{language}", "en")
    assert system_prompt("ko") == system_prompt("ko", "v1")
    config = LlmConfig.from_settings(_settings(chat_judgment_facts=False, chat_prompt_version="v1"))
    assert (config.prompt_version, config.prompt_version_id) == ("v1", "chat-v1")
    assert config.judgment_facts is False


def test_default_is_judgment_facts_v2_haiku() -> None:
    # Model and judgment facts: judgment facts comparison (2026-11-12).
    # Prompt v2: v1.1 vs v2 regression run (2026-11-15).
    settings = _settings()
    assert settings.chat_judgment_facts is True
    assert settings.chat_prompt_version == "v4.1"
    assert settings.chat_model == "claude-haiku-5-5"
    config = LlmConfig.from_settings(settings)
    assert config.model == "claude-haiku-5-5"
    assert config.judgment_facts is True
    assert (config.prompt_version, config.prompt_version_id) == ("v4.1", "chat-v4.1")


def test_v2_is_v1_1_plus_visa_rules() -> None:
    # v2는 복사본이 아니라 v1.1 + visa_rules.md로 만든다.
    # 바뀌는 v1.1 줄은 "비자 점수는 아직 안 됨" 하나다.
    v11 = system_prompt("en", "v1.1").splitlines()
    v2 = system_prompt("en", "v2").splitlines()
    visa = (PROMPTS / "visa_rules.md").read_text(encoding="utf-8").splitlines()
    assert V11_VISA_UNAVAILABLE in v11
    assert not (PROMPTS / "system_v2.md").exists()
    expected = [V2_PRODUCTS if line == V11_VISA_UNAVAILABLE else line for line in v11]
    assert v2 == [*expected, "", *visa]
    # v1.1의 숫자·판단 규칙은 그대로 남는다.
    assert V11_NUMBERS in v2
    assert V11_JUDGMENT in v2
    for name in ("calculate_e74_score", "search_visa_policies", "HiKorea (1345)", "pre-check"):
        assert any(name in line for line in visa)


def test_v2_fails_fast_when_v1_1_line_moves() -> None:
    with pytest.raises(ValueError, match="visa-unavailable line"):
        _with_visa_rules("Rules:\n- something else", "- visa")


V2_REMITTANCE_MENU = (
    "- Never send money. For remittances, guide the user to the overseas remittance menu."
)
V3_REMITTANCE_RULES = "- Never send money. For remittances, follow the remittance rules below."


def test_v3_is_v2_plus_remittance_rules() -> None:
    # v3는 v2 + remittance_rules.md. 바뀌는 v2 줄은 "송금 메뉴로 안내" 하나다.
    v2 = system_prompt("en", "v2").splitlines()
    v3 = system_prompt("en", "v3").splitlines()
    rules = (PROMPTS / "remittance_rules.md").read_text(encoding="utf-8").splitlines()
    assert V2_REMITTANCE_MENU in v2
    expected = [V3_REMITTANCE_RULES if line == V2_REMITTANCE_MENU else line for line in v2]
    assert v3 == [*expected, "", *rules]
    # v1.1 숫자·판단 규칙과 v2 비자 규칙은 그대로 남는다.
    assert V11_NUMBERS in v3 and V11_JUDGMENT in v3
    assert any("calculate_e74_score" in line for line in v3)
    text = " ".join(rules)
    for phrase in (
        "get_remittance_recipients",
        "quote_remittance",
        "ask about only one of them",
        "confirmation card",
        "You cannot skip the confirmation",
        "did not ask to send money",
        "Do not use bold",
    ):
        assert phrase in text, phrase


@pytest.mark.parametrize(
    ("version", "rule"),
    [
        (
            "v3.1",
            "- When you mention the exchange rate, copy appliedRate.rateText exactly. "
            "Do not restate it in other units or directions.",
        ),
        (
            "v3.2",
            "- Do not state the exchange rate in remittance replies. The confirmation card shows "
            "it. Say only who receives how much.",
        ),
    ],
)
def test_v3_x_is_v3_plus_one_rate_rule(version: str, rule: str) -> None:
    # remittance_rules.md는 그대로 두고 v3 끝(송금 규칙 절)에 한 줄만 더한다.
    v3 = system_prompt("en", "v3").splitlines()
    assert system_prompt("en", version).splitlines() == [*v3, rule]
    assert rule not in (PROMPTS / "remittance_rules.md").read_text(encoding="utf-8")
    config = LlmConfig.from_settings(_settings(chat_prompt_version=version))
    assert config.prompt_version_id == f"chat-{version}"


def test_default_is_v4_1() -> None:
    assert _settings().chat_prompt_version == "v4.1"


def test_v3_fails_fast_when_v2_line_moves() -> None:
    with pytest.raises(ValueError, match="remittance-menu line"):
        _with_remittance_rules("Rules:\n- something else", "- remit")


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


# 버전별 완성된 프롬프트(en)의 SHA-256. 채팅 기록(chat_messages.prompt_version)과 측정 결과가
# 가리키는 글을 고정한다. system_v1_1.md·visa_rules.md·remittance_rules.md는 여러 버전이 같이 쓰므로
# 고치면 기본값(v3)을 포함한 여러 버전이 같은 이름으로 함께 바뀐다.
PROMPT_SHA256 = {
    "v1": "50f9632829d5f74b8764db7b7aea75527b5b05813e0b0eb5be3dc5e31be6ef48",
    "v1.1": "97b95203c491a97bd64cf0ea32f543f68a304085b3c18abf30b72e5b4d02c0c2",
    "v2": "6376736a071df227659abe25b3eade6ff9c0511052e6e67f9ced4809d7bf6c34",
    "v3": "81694ab609924880683ca239749d2068d355c35e0f13e42f0dcdbf9664f79a65",
    "v3.1": "80c7f20adb3219077541d060898ccc87572b79fbc47772775d5c4d901da86ec9",
    "v3.2": "e62f7dbb84c4eebfc244dfa294c722f6a8f615088c89e061f8d53a30ed36a8c7",
    "v4": "fc0e4ef7e61eef927cd1485d3a6e6a1e0c38c770ea0d8cdc068a5c10f632ee32",
    "v4.1": "396e508c6213de778325464d2c5c847890c836817552dbbb8531545299c1d4e5",
}


def test_prompt_text_is_fixed_per_version() -> None:
    # 실패하면 값을 고치기 전에, 바뀐 버전을 그대로 고칠지 새 버전을 만들지 먼저 정한다.
    actual = {
        version: hashlib.sha256(system_prompt("en", version).encode("utf-8")).hexdigest()
        for version in PROMPT_VERSIONS
    }
    assert actual == PROMPT_SHA256


def test_prompt_rules_and_attached_tools_agree() -> None:
    # 설정 값·프롬프트·도구 연결(app/api/routes/chat.py)은 따로 적혀 있어 손으로 맞춰야 한다.
    # 프롬프트가 도구 이름을 말하는 버전에만 그 도구가 붙어야 한다.
    assert set(get_args(ChatPromptVersion)) == set(PROMPT_VERSIONS)
    for version in PROMPT_VERSIONS:
        prompt = system_prompt("en", version)
        visa = any(name in prompt for name in VISA_TOOL_NAMES)
        remittance = any(name in prompt for name in REMITTANCE_TOOL_NAMES)
        assert visa == (version in VISA_PROMPT_VERSIONS), version
        assert remittance == (version in REMITTANCE_PROMPT_VERSIONS), version
        product = RECOMMEND_SAVINGS_PRODUCTS in prompt
        assert product == (version in PRODUCT_PROMPT_VERSIONS), version


V4_PRODUCTS = (
    "- Recommend only savings and time deposit products, following the product rules below. "
    "Other product recommendations (cards, loans, checking accounts) are not available in chat "
    "yet. Say so briefly."
)


def test_v4_is_v3_plus_product_rules() -> None:
    # v4는 v3 + product_rules.md. 바뀌는 v3 줄은 "상품 추천은 아직 안 됨" 하나다.
    v3 = system_prompt("en", "v3").splitlines()
    v4 = system_prompt("en", "v4").splitlines()
    rules = (PROMPTS / "product_rules.md").read_text(encoding="utf-8").splitlines()
    assert V2_PRODUCTS in v3
    expected = [V4_PRODUCTS if line == V2_PRODUCTS else line for line in v3]
    assert v4 == [*expected, "", *rules]
    text = " ".join(rules)
    for phrase in (
        "recommend_savings_products",
        "queryKo",
        "REQUEST_EXCEEDS_SURPLUS",
        "STAY_TOO_SHORT",
        "NO_MATCHING_PRODUCT",
        "Never state interest rates",
        '"explicit"',
    ):
        assert phrase in text, phrase


def test_v4_fails_fast_when_v3_line_moves() -> None:
    with pytest.raises(ValueError, match="products line"):
        _with_product_rules("Rules:\n- something else", "- products")


def test_v4_1_is_v4_with_one_stay_sentence() -> None:
    # v4.1(v4 1차 측정 뒤 수정): STAY_TOO_SHORT 안내 문장 하나만 바뀐다.
    v4, v41 = system_prompt("en", "v4"), system_prompt("en", "v4.1")
    assert V4_STAY_RULE in v4 and V4_STAY_RULE not in v41
    assert v41 == v4.replace(V4_STAY_RULE, V4_1_STAY_RULE)
    assert "extending the stay or the visa" in V4_1_STAY_RULE
    with pytest.raises(ValueError, match="STAY_TOO_SHORT rule"):
        _with_stay_rule("no such rule")

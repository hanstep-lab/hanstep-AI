"""Chat prompt versions: v1 unchanged, v1.1 two lines, v2 visa, v3 remittance, v4 product, v4.4 FX.

v4.4 = v4.2 + exchange rate rules (fx_rules.md). v4.4d = v4.4 + answer display rules (#116).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import get_args

import pytest

from app.api.routes.chat import (
    FINANCE_POLICY_PROMPT_VERSIONS,
    FX_PROMPT_VERSIONS,
    PRODUCT_PROMPT_VERSIONS,
    REMITTANCE_PROMPT_VERSIONS,
    VISA_PROMPT_VERSIONS,
)
from app.chat.finance_policy_tools import SEARCH_FINANCE_POLICY
from app.chat.fx_tools import FX_RATE_SUMMARY
from app.chat.product_tools import RECOMMEND_SAVINGS_PRODUCTS
from app.chat.prompts import (
    NO_BOLD_LINE,
    PROMPT_VERSION,
    PROMPT_VERSIONS,
    V4_1_STAY_RULE,
    V4_4D_CHANGES,
    V4_STAY_RULE,
    _with_display_rules,
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
        "v4.2": "chat-v4.2",
        "v4.3a": "chat-v4.3a",
        "v4.4": "chat-v4.4",
        "v4.4d": "chat-v4.4d",
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
    assert settings.chat_prompt_version == "v4.4d"
    assert settings.chat_model == "claude-haiku-5-5"
    config = LlmConfig.from_settings(settings)
    assert config.model == "claude-haiku-5-5"
    assert config.judgment_facts is True
    assert (config.prompt_version, config.prompt_version_id) == ("v4.4d", "chat-v4.4d")


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


def test_default_is_v4_4d() -> None:
    # #116: AI 코드 기본값 v4.1 → v4.4d. 운영 값은 hanstep-backend compose.prod.yml(v4.4)이 정한다.
    assert _settings().chat_prompt_version == "v4.4d"


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
# 2026-11-17(#77): 비자·체류 규칙에 한정하고 상품·송금은 제외해 v2~v4.1 해시 갱신.
PROMPT_SHA256 = {
    "v1": "50f9632829d5f74b8764db7b7aea75527b5b05813e0b0eb5be3dc5e31be6ef48",
    "v1.1": "97b95203c491a97bd64cf0ea32f543f68a304085b3c18abf30b72e5b4d02c0c2",
    "v2": "c964dadba0fc6fc114bee3d79748ee13737169a1cd2c2641e8a2dd5cb235eee6",
    "v3": "afcd6901865f703db374f327ae73fd3ba932043bcefbed5b0c56f959354c98c0",
    "v3.1": "360206c1d6de37092cb1b5f1baaa5d696e07bb67ec17c0ffa6c9af2423ec3305",
    "v3.2": "7ab4be64bb55670b4aea13746b2bf734d15ab7595c02f1fb45364a930b2544d0",
    "v4": "15f6bcf6433a52edd8da0b3c7677832fd84de5aa6069a81c67ab3aeece297252",
    "v4.1": "6348dd67a95b15f8cd375df4d6c543c987336b84aa83a93df082c8cf5001a0bf",
    "v4.2": "8edda4074b53ece8d6cc30c69ec61e70c82ff38dd4ea5ba1b14f67e8591df51d",
    "v4.3a": "ac9c014344e4409b0553ca5f3006010a4e2c1c51d72da32f3a795c7938b4165c",
    # 2026-11-18(#91): v4.2 본문 그대로 + 환율 진단 규칙(fx_rules.md).
    "v4.4": "61f665e249634c23bb469820f82a9d354b7d57c83c5460af433a0d7449541f55",
    # 2026-11-20(#116): v4.4 + 답변 표시 규칙(영어 규칙, en·vi·zh). ko는 PROMPT_SHA256_KO.
    "v4.4d": "6d72bb2bc1a76fc4bb8b3083394e0a41abdb7889c0320bfbd44f9298beec5e95",
}
# 표시 규칙이 언어별로 다른 버전의 한국어(ko) 프롬프트.
PROMPT_SHA256_KO = {
    "v4.4d": "e69b2c08fae002baeafa8aae841abae6193cf7abb8688b20d52d49587abc0903",
}


def test_prompt_text_is_fixed_per_version() -> None:
    # 실패하면 값을 고치기 전에, 바뀐 버전을 그대로 고칠지 새 버전을 만들지 먼저 정한다.
    actual = {
        version: hashlib.sha256(system_prompt("en", version).encode("utf-8")).hexdigest()
        for version in PROMPT_VERSIONS
    }
    assert actual == PROMPT_SHA256
    actual_ko = {
        version: hashlib.sha256(system_prompt("ko", version).encode("utf-8")).hexdigest()
        for version in PROMPT_SHA256_KO
    }
    assert actual_ko == PROMPT_SHA256_KO


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
        policy = SEARCH_FINANCE_POLICY in prompt
        assert policy == (version in FINANCE_POLICY_PROMPT_VERSIONS), version
        assert (FX_RATE_SUMMARY in prompt) == (version in FX_PROMPT_VERSIONS), version


def test_v4_4_is_v4_2_plus_fx_rules() -> None:
    # v4.4(#91, 리뷰 결정): v4.2의 줄은 그대로 두고 끝에 환율 진단 규칙 절만 더한다.
    # 환율 진단 도구는 v4.4에만 붙고, 기본값은 아직 v4.1이다(측정을 보고 정한다).
    v42, v44 = system_prompt("en", "v4.2"), system_prompt("en", "v4.4")
    fx_rules = (PROMPTS / "fx_rules.md").read_text(encoding="utf-8")
    assert v44 == v42.rstrip("\n") + "\n\n" + fx_rules
    assert FX_PROMPT_VERSIONS == frozenset({"v4.4", "v4.4d"})
    config = LlmConfig.from_settings(_settings(chat_prompt_version="v4.4"))
    assert config.prompt_version_id == "chat-v4.4"
    for rule in (
        "call fx_rate_summary first",
        "never forecast rates",
        '"market reference rate" for MARKET_REF',
        "If isMock is true, say clearly that these rates are demo data",
        "If stale is true, stress the as-of date",
        "insufficientDataPeriods",
    ):
        assert rule in v44, rule


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


def test_v4_2_is_v4_1_plus_finance_policy_rules() -> None:
    # v4.2(#88): v4.1 문구는 그대로 두고 금융 정책 규칙을 끝에 더한다. 기본값은 v4.1 그대로다.
    v41 = system_prompt("en", "v4.1").splitlines()
    v42 = system_prompt("en", "v4.2").splitlines()
    rules = (PROMPTS / "finance_policy_rules.md").read_text(encoding="utf-8").splitlines()
    assert v42 == [*v41, "", *rules]
    text = " ".join(rules)
    for phrase in (
        "search_finance_policy",
        "queryKo in Korean",
        "foreign residents",
        "effective date",
        "Under the regulation",
        "do not guess",
    ):
        assert phrase in text, phrase
    config = LlmConfig.from_settings(_settings(chat_prompt_version="v4.2"))
    assert config.prompt_version_id == "chat-v4.2"


def test_v4_3a_is_v4_2_plus_direction_rule() -> None:
    # v4.3a(#88 스펙 가드 ①): v4.2 문구 그대로 + 규정 규칙 끝에 한 줄. 도구 구성은 v4.2와 같다.
    from app.chat.prompts import DIRECTION_RULE

    v42 = system_prompt("en", "v4.2").splitlines()
    assert system_prompt("en", "v4.3a").splitlines() == [*v42, DIRECTION_RULE]
    for phrase in ("수령", "지급", "receiving", "sending", "applies to"):
        assert phrase in DIRECTION_RULE, phrase
    config = LlmConfig.from_settings(_settings(chat_prompt_version="v4.3a"))
    assert config.prompt_version_id == "chat-v4.3a"


@pytest.mark.parametrize("language", ["ko", "en"])
def test_v4_4d_is_v4_4_with_display_rules(language: str) -> None:
    # v4.4d(#116, display): v4.4 기준 갈래(v4.3a와 같은 방식). 바뀌는 v4.4 줄은 V4_4D_CHANGES뿐이고,
    # 볼드 금지 두 줄(송금·환율)은 빠지며, 끝에 사용자 언어의 표시 규칙이 붙는다.
    v44 = system_prompt(language, "v4.4").splitlines()
    rules = (PROMPTS / f"display_rules_{language}.md").read_text(encoding="utf-8")
    changes = {
        old.replace("{language}", language): new.replace("{language}", language)
        for old, new, _ in V4_4D_CHANGES
    }
    assert v44.count(NO_BOLD_LINE) == 2
    assert all(old in v44 for old in changes)
    expected = [changes.get(line, line) for line in v44 if line != NO_BOLD_LINE]
    assert system_prompt(language, "v4.4d").splitlines() == [
        *expected,
        "",
        *rules.replace("{language}", language).splitlines(),
    ]
    # v4.4 글은 언어 코드만 다르다(표시 규칙이 없는 버전).
    assert system_prompt("ko", "v4.4") == system_prompt("en", "v4.4").replace('"en"', '"ko"')


def test_v4_4d_rules_by_language() -> None:
    ko, en = system_prompt("ko", "v4.4d"), system_prompt("en", "v4.4d")
    # ko: 원화는 "원"만, 외화는 처음 한 번 "미국 달러(USD)". en: 통화 코드.
    for phrase in ("followed by 원", "Never write KRW, (KRW)", "미국 달러(USD)", "100동당"):
        assert phrase in ko and phrase not in en, phrase
    for phrase in ("1 USD = 1,330.80 KRW", "KRW 700,000", "not won"):
        assert phrase in en and phrase not in ko, phrase
    # 공통: 결론 첫 줄만 볼드, 숫자 목록, 출처·기준 시각 줄, 표시값 그대로, 영업일 수 빼기.
    for text in (ko, en):
        for phrase in (
            "wrap only that sentence in **",
            'each line starting with "- "',
            "source and the as-of date and time",
            "copy that text exactly",
            "Do not mention the number of business days",
            "rateText",
            "basisText",
        ):
            assert phrase in text, phrase
        assert NO_BOLD_LINE not in text
        assert "currency codes (KRW) as they are" not in text
        assert "3 to 5 short sentences" not in text
    # vi·zh는 영어 표시 규칙을 받고, 답변 언어 줄만 그 언어 코드다.
    for language in ("vi", "zh"):
        assert system_prompt(language, "v4.4d") == en.replace('"en"', f'"{language}"')
    config = LlmConfig.from_settings(_settings(chat_prompt_version="v4.4d"))
    assert config.prompt_version_id == "chat-v4.4d"


def test_v4_4d_fails_fast_when_a_v4_4_line_moves() -> None:
    with pytest.raises(ValueError, match="language line"):
        _with_display_rules("Rules:\n- something else", "Display rules:")
    base = system_prompt("en", "v4.4").replace('"en"', '"{language}"')
    with pytest.raises(ValueError, match="no-bold line exactly twice"):
        _with_display_rules(base.replace(NO_BOLD_LINE + "\n", "", 1), "Display rules:")

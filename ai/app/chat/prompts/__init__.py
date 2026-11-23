"""Versioned chat system prompts (CHAT_PROMPT_VERSION picks one;
the default is Settings.chat_prompt_version).
"""

from __future__ import annotations

from pathlib import Path

from app.chat.display import DisplayLanguage, display_language

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
    "v4.4": "chat-v4.4",
    "v4.5": "chat-v4.5",
    "v4.4d": "chat-v4.4d",
    "v4.6": "chat-v4.6",
    "v4.7": "chat-v4.7",
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
    """v4.2 + 환율 진단 규칙(fx_rules.md, #91). v4.2의 줄은 바꾸지 않고 끝에 절을 더한다."""
    return base.rstrip("\n") + "\n\n" + fx_rules


# v4.4(#91, 리뷰 결정): v4.2 본문 그대로 + 환율 진단 규칙. v4.3a의 방향 규칙은 넣지 않는다.
# 환율 진단 도구 fx_rate_summary는 v4.4에서만 붙는다.
_SYSTEM["v4.4"] = _with_fx_rules(
    _SYSTEM["v4.2"], (_DIR / "fx_rules.md").read_text(encoding="utf-8")
)
# v4.5(#114): v4.4 본문 그대로 + 목표·출처·상품 카드 규칙(goal_rules.md). v4.4의 줄은 바꾸지 않는다.
# product_rules.md의 '금리는 앱·지점에서 확인' 줄은 그대로 두고,
# 카드가 붙는 v4.5에서만 이 절이 대신한다.
# 상품 추천 카드(PRODUCT_RECOMMEND_CARD)는 v4.5에서만 붙는다.
_SYSTEM["v4.5"] = (
    _SYSTEM["v4.4"].rstrip("\n") + "\n\n" + (_DIR / "goal_rules.md").read_text(encoding="utf-8")
)


# 답변 표시 규칙(#116): v4.4 본문의 줄 몇 개를 바꾸거나 빼고, 끝에 답변 표시 규칙
# (display_rules_ko.md·display_rules_en.md)을 더한다. 표시 규칙은 사용자 언어로 고른다: ko면 한국어
# 규칙, 그 밖(en·vi·zh)은 영어 규칙. 환율 도구는 LLM에 표시값(rateText 등)을 더 준다.
# - v4.6 = v4.5 + 표시 규칙 + 상품 카드 표시 규칙(#119, 아래). 도구 묶음은 v4.5와 같다(환율 진단·
#   상품 카드 포함).
# - v4.4d = v4.4 + 표시 규칙(v4.3a와 같은 갈래). 11/20 측정 재현용으로 남긴다.
# v4.4·v4.5·fx_rules.md·goal_rules.md 문구는 바꾸지 않는다. 바꿀 줄은 v4.4 본문에 있고, v4.5도
# v4.4 본문을 그대로 담는다.
V4_4_LANGUAGE_LINE = (
    '- Write the answer in the language with code "{language}". Keep official names and currency '
    "codes (KRW) as they are."
)
DISPLAY_LANGUAGE_LINE = (
    '- Write the answer in the language with code "{language}". Keep official names as they are. '
    "Write numbers and currencies and lay out the answer as the display rules at the end say."
)
V4_4_LENGTH_LINE = "- Answer in 3 to 5 short sentences in plain words."
DISPLAY_LENGTH_LINE = "- Keep the answer short and in plain words."
V4_4_FX_NUMBERS_LINE = (
    "- Use only numbers that appear in the fx_rate_summary result, exactly as given (same digits "
    "and decimals). Do not calculate, round or convert any number."
)
DISPLAY_FX_NUMBERS_LINE = (
    "- Use only numbers that appear in the fx_rate_summary result. Copy its display texts "
    "exactly: rateText for the rate, changeText for the change from the previous business day, "
    "and averageText and differenceText for each period average. Do not calculate, round or "
    "convert any number."
)
V4_4_FX_RATE_LINE = (
    "- Rates are KRW per `unit` of the currency. When you give a rate, write it as "
    '"<unit> <currency> = <rate> KRW".'
)
DISPLAY_FX_RATE_LINE = (
    "- Rates are KRW per `unit` of the currency. Write them as the display rules at the end say."
)
V4_4_FX_BASIS_LINE = (
    "- Give the basis with the numbers: the source, the as-of date (asOf) and, for each average "
    "you mention, the number of business days in it."
)
DISPLAY_FX_BASIS_LINE = (
    "- In the last line, give the basis of each currency you mention: its sourceText and "
    "basisText. Do not mention the number of business days in an average."
)
V4_4_FX_SOURCE_LINE = (
    '- Source names: say "market reference rate" for MARKET_REF (ECB-based, not a remittance '
    'rate) and "Korea Eximbank published rate" for KOREAEXIM.'
)
DISPLAY_FX_SOURCE_LINE = (
    "- Name the source with sourceText. MARKET_REF is an ECB-based market reference rate, not a "
    "remittance rate; KOREAEXIM is the rate the Korea Eximbank publishes."
)
# v4.4에 두 번(송금 규칙·환율 규칙) 있다. 표시 규칙 버전은 둘 다 빼고
# 표시 규칙의 볼드 규칙을 따른다.
NO_BOLD_LINE = "- Do not use bold or any other markdown emphasis."
# v4.4 줄 → 표시 규칙 버전의 줄. 바꿀 줄이 바탕 글에 정확히 한 번 있지 않으면 시작할 때 실패한다.
DISPLAY_CHANGES = (
    (V4_4_LANGUAGE_LINE, DISPLAY_LANGUAGE_LINE, "language"),
    (V4_4_LENGTH_LINE, DISPLAY_LENGTH_LINE, "answer-length"),
    (V4_4_FX_NUMBERS_LINE, DISPLAY_FX_NUMBERS_LINE, "fx-numbers"),
    (V4_4_FX_RATE_LINE, DISPLAY_FX_RATE_LINE, "fx-rate"),
    (V4_4_FX_BASIS_LINE, DISPLAY_FX_BASIS_LINE, "fx-basis"),
    (V4_4_FX_SOURCE_LINE, DISPLAY_FX_SOURCE_LINE, "fx-source"),
)


def _with_display_rules(base: str, display_rules: str) -> str:
    """바탕 글(v4.4·v4.5)의 DISPLAY_CHANGES 줄을 바꾸고, 볼드 금지 두 줄을 빼고, 표시 규칙을 더한다.

    바꿀 줄이 바탕 글에 정확히 한 번 있지 않거나 볼드 금지 줄이 두 번이 아니면 실패한다.
    """
    text = base
    for old, new, what in DISPLAY_CHANGES:
        text = "\n".join(_replace_line(text, old, new, what))
    lines = text.splitlines()
    if lines.count(NO_BOLD_LINE) != 2:
        raise ValueError("the base prompt must contain the no-bold line exactly twice")
    lines = [line for line in lines if line != NO_BOLD_LINE]
    return "\n".join(lines).rstrip("\n") + "\n\n" + display_rules


_DISPLAY_LANGUAGES: tuple[DisplayLanguage, ...] = ("ko", "en")
_DISPLAY_RULES = {
    language: (_DIR / f"display_rules_{language}.md").read_text(encoding="utf-8")
    for language in _DISPLAY_LANGUAGES
}


def _display_versions(base: str, extra: str = "") -> dict[DisplayLanguage, str]:
    """언어별 표시 규칙 버전. extra가 있으면 표시 규칙 뒤에 그 언어의 extra_{언어}.md를 붙인다."""
    versions = {}
    for language in _DISPLAY_LANGUAGES:
        text = _with_display_rules(base, _DISPLAY_RULES[language])
        if extra:
            rules = (_DIR / f"{extra}_{language}.md").read_text(encoding="utf-8")
            text = text.rstrip("\n") + "\n\n" + rules
        versions[language] = text
    return versions


# v4.6 상품 카드 표시 규칙(#119): 카드에 있는 상품 이름·기간·금액 범위·금리를 본문에서 다시 나열하지
# 않는다. v4.6에서만 바꾼다.
# - 표시 규칙 파일(display_rules_*.md)은 v4.4d도 쓰므로 고치지 않는다. v4.4d에는 상품 카드가 없다.
# - 표시 규칙 뒤에 상품 카드 표시 규칙(display_product_card_ko.md·_en.md)을 붙인다.
# - 상품 규칙(product_rules.md) 가운데 이 규칙과 부딪히는 두 줄을 v4.6에서만 바꾼다.
#   product_rules.md 파일과 v4.5 문구는 그대로다.
PRODUCT_FACTS_LINE = (
    "- Recommend only the products in the tool result, in the same order and with the names as "
    "given. For each product, give one or two facts from its terms or evidence, such as the term "
    "or the monthly amount range."
)
CARD_PRODUCT_FACTS_LINE = (
    "- Recommend only the products in the tool result, with the names as given. The product card "
    "lists them; write the answer as the product card display rules at the end say."
)
PRODUCT_ORDER_LINE = (
    "- Do not call any product the best or the most profitable; the order is not a quality "
    "ranking. Do not promise approval, eligibility or tax benefits."
)
CARD_PRODUCT_ORDER_LINE = (
    "- Do not call any product the best or the most profitable. Do not promise approval, "
    "eligibility or tax benefits."
)
PRODUCT_CARD_CHANGES = (
    (PRODUCT_FACTS_LINE, CARD_PRODUCT_FACTS_LINE, "product-facts"),
    (PRODUCT_ORDER_LINE, CARD_PRODUCT_ORDER_LINE, "product-order"),
)


def _with_product_card_lines(base: str) -> str:
    """바탕 글(v4.5)의 PRODUCT_CARD_CHANGES 줄을 바꾼다. 바꿀 줄이 한 번 있지 않으면 실패한다."""
    text = base
    for old, new, what in PRODUCT_CARD_CHANGES:
        text = "\n".join(_replace_line(text, old, new, what))
    return text


# 표시 규칙이 사용자 언어별로 다른 버전. system_prompt가 language로 고른다.
_SYSTEM_BY_DISPLAY: dict[str, dict[DisplayLanguage, str]] = {
    "v4.4d": _display_versions(_SYSTEM["v4.4"]),
    "v4.6": _display_versions(
        _with_product_card_lines(_SYSTEM["v4.5"]), extra="display_product_card"
    ),
}


# v4.7(#127): v4.6 그대로 + 비자 로드맵 규칙(roadmap_rules.md). 로드맵 도구 get_visa_roadmap은
# v4.7에서만 붙는다. 부딪히는 두 줄만 v4.7에서 바꾼다. visa_rules.md·goal_rules.md 파일과
# v4.6 이하 문구는 그대로다.
# - 점수 규칙: '점수 올리는 법'은 로드맵으로 옮긴다. calculate_e74_score는 점수·신청 가능 여부만.
# - 목표 규칙(SETTLEMENT): 열린 조언 질문에서는 로드맵 도구도 부르지 않는다(v4.6과 같은 동작).
E74_SCORE_LINE = (
    "- For any question about the user's own E-7-4 score, eligibility or how to raise the score, "
    "call calculate_e74_score first. Do not write any text before the tool call."
)
ROADMAP_E74_SCORE_LINE = (
    "- For any question about the user's own E-7-4 score or eligibility, call calculate_e74_score "
    "first. Do not write any text before the tool call. For how to raise the score, follow the "
    "visa roadmap rules below."
)
SETTLEMENT_GOAL_LINE = (
    "  - SETTLEMENT: suggest checking the E-7-4 score or the '비자 로드맵' (visa roadmap) menu. "
    "Do not call calculate_e74_score unless the user asks for the score."
)
ROADMAP_SETTLEMENT_GOAL_LINE = (
    "  - SETTLEMENT: suggest checking the E-7-4 score or the '비자 로드맵' (visa roadmap) menu. "
    "Do not call calculate_e74_score or get_visa_roadmap unless the user asks for the score or "
    "the roadmap."
)
ROADMAP_CHANGES = (
    (E74_SCORE_LINE, ROADMAP_E74_SCORE_LINE, "e74-score"),
    (SETTLEMENT_GOAL_LINE, ROADMAP_SETTLEMENT_GOAL_LINE, "settlement-goal"),
)
_ROADMAP_RULES = (_DIR / "roadmap_rules.md").read_text(encoding="utf-8")


def _with_roadmap_rules(base: str) -> str:
    """바탕 글(v4.6)의 ROADMAP_CHANGES 줄을 바꾸고 끝에 로드맵 규칙을 더한다.

    바꿀 줄이 바탕 글에 정확히 한 번 있지 않으면 시작할 때 실패한다.
    """
    text = base
    for old, new, what in ROADMAP_CHANGES:
        text = "\n".join(_replace_line(text, old, new, what))
    return text.rstrip("\n") + "\n\n" + _ROADMAP_RULES


_SYSTEM_BY_DISPLAY["v4.7"] = {
    language: _with_roadmap_rules(text) for language, text in _SYSTEM_BY_DISPLAY["v4.6"].items()
}


def system_prompt(language: str, version: str = "v1") -> str:
    by_display = _SYSTEM_BY_DISPLAY.get(version)
    text = _SYSTEM[version] if by_display is None else by_display[display_language(language)]
    return text.replace("{language}", language)

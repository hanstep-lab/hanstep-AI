"""Chat agent measurement: latency, tokens, cost and unsupported numbers on the same questions.

Not part of pytest or CI. Calls a running server; it never calls Anthropic itself.

    # INT-05 directly (tokens and agent.metrics); server needs CHAT_ALLOW_CONFIG_OVERRIDE=true
    # when --variants is given. --llm-mode says what the server runs (default fake).
    uv run python scripts/evaluate_chat.py --target ai --llm-mode anthropic --questions F01,F03 \
        --output artifacts/chat/haiku_check.jsonl
    # Spring CHT-04 end to end (latency only)
    uv run python scripts/evaluate_chat.py --target spring --jwt <token> --subset latency \
        --output artifacts/chat/spring_latency.jsonl
    # Re-score a saved run without any API call
    uv run python scripts/evaluate_chat.py --rescore artifacts/chat/run.jsonl \
        --output artifacts/chat/run_rescored.jsonl
    # Remittance clarification set (specs/chat/remit_questions_v1.json, prompt v3)
    uv run python scripts/evaluate_chat.py --question-set remit --llm-mode anthropic \
        --variants specs/chat/variants_remit_v1.json --output artifacts/chat/remit_v1.jsonl

Remittance set (--question-set remit): an ambiguous question whose first answer did not proceed
gets a second turn with its followUpReply (history: the question and the first answer). Each row
gets a remitCheck from tool arguments, done.blocks and answer rules only: correct, missed
clarification (proceeded while ambiguous), unnecessary question (asked while clear), wrong
guess (recipient, currency or amount), out-of-scope handling, turns to completion. Anything the
rules cannot settle is marked needsReview and listed for a person in the .md.
--remit-questions <path> runs another file with the same schema (checked before any call; the
path may be outside the repo, e.g. a hold-out set). Its expected values after the second turn are
unknown, so those are left to a person; --rescore never re-reads such a file.

Writes one JSONL line per call as it goes (the first line holds the conditions) and a summary
markdown next to it (same name, .md). Variants run round-robin inside each question and
repeat, so time-of-day drift spreads evenly. --max-usd stops before a call that would likely
cross the budget; runs that end in agent.error count too (usage from agent.metrics). With
--llm-mode fake the run stops at once if the server answers with a real model or real tokens.
Questions with referenceValues get a referenceCheck (reference / wrong_number / no_value), and
each row keeps the arrival ms of every delta (deltaTimesMs) for replaying the stream later.
Judgment questions (expectedConclusion) are listed with their full answers for a person to grade,
in the .md only: stdout gets the summary without answers (they can hold a user's finances).
Their numbers are counted apart from the unsupported-number count, since a right answer may
calculate one. Expected conclusions are worked out on the demo fixture; a run whose tool data
differs from it is marked. --rescore re-runs the checks but keeps the expectedConclusion and
referenceValues saved in each row (the basis that run was graded on); only the expected tools are
re-read from the current question set (noted as expectedToolsFrom).
Numbers from the remittance tools (recipients, rate, quote card) count as supported when the row
called one of them. Such rows also get a rateUnit verdict on how the answer states the tool's
exchange rate (correct / wrong direction or unit / no_unit / unclear, unclear goes to a person).
With judgmentFacts on, the number check also allows the judgment facts the server added
(app/chat/judgment.py on the same summary and question).
With --target ai the run stops if the server's model, judgmentFacts or prompt version differ
from the variant (e.g. X-Chat-Config ignored because CHAT_ALLOW_CONFIG_OVERRIDE is off).
Every answer gets a languageCheck (#106, scripts/_lang_check.py): Hangul mixed into an answer
for a non-Korean user, with the allowlist in specs/chat/answer_language_allowlist_v1.json.
--language (ai target only) sets the user's language sent to the agent (default en); the answer
language set is --question-set lang (specs/chat/answer_language_questions_v1.json).
The API key and internal tokens are never printed or written.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import re
import statistics
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401
import _fx_check
import _lang_check
import _product_check
import httpx

from app.api.routes.chat import PRODUCT_PROMPT_VERSIONS, REMITTANCE_PROMPT_VERSIONS
from app.chat.fake_llm import FAKE_MODEL_ID
from app.chat.fx_tools import FX_RATE_SUMMARY, FX_SUMMARY_CARD
from app.chat.judgment import judgment_facts
from app.chat.number_check import check_answer, rounded_values, scan_numbers
from app.chat.prompts import PROMPT_VERSIONS, system_prompt
from app.chat.remittance_tools import FIXTURE as REMITTANCE_FIXTURE
from app.chat.remittance_tools import (
    GET_REMITTANCE_RECIPIENTS,
    QUOTE_REMITTANCE,
    REMITTANCE_TOOL_NAMES,
)
from app.chat.tools import FIXTURE, summarize
from app.core.config import Settings, get_settings
from app.integrations.spring_client import SpringInternalClient
from app.rag.cli import EXIT_BLOCKED, EXIT_FAILED, EXIT_OK

AI_ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = AI_ROOT / "specs" / "chat" / "eval_questions_v1.json"
REMIT_QUESTIONS = AI_ROOT / "specs" / "chat" / "remit_questions_v1.json"
PRODUCT_QUESTIONS = AI_ROOT / "specs" / "chat" / "product_questions_v1.json"
# 금융 정책 RAG(#88) 기준선: 해외송금 규정 질문. 답변 채점은 scripts/finance_policy_review.py.
POLICY_QUESTIONS = AI_ROOT / "specs" / "rag" / "finance_policy_questions_v1.json"
# 프롬프트 v4.4 전환 측정(#101): 환율 진단(FX)·E-7-4 점수(E)·송금 카드(R) 문항. eval과 같은 형식.
V44_QUESTIONS = AI_ROOT / "specs" / "chat" / "v44_questions_v1.json"
# 답변 언어 측정(#106): E-7-4·비자 정책·금융 정책·대조군(환율·송금). 언어는 --language.
LANG_QUESTIONS = AI_ROOT / "specs" / "chat" / "answer_language_questions_v1.json"
# E-7-4 도구 LLM 입력 영어화 측정(#108): 사용자 본인의 E-7-4 점수·신청·개선·서류·가정 계산 문항.
E74_EN_QUESTIONS = AI_ROOT / "specs" / "chat" / "e74_llm_en_questions_v1.json"
QUESTION_SETS = {
    "eval": QUESTIONS,
    "remit": REMIT_QUESTIONS,
    "product": PRODUCT_QUESTIONS,
    "policy": POLICY_QUESTIONS,
    "v44": V44_QUESTIONS,
    "lang": LANG_QUESTIONS,
    "e74en": E74_EN_QUESTIONS,
}
# 상위 버전에 기대 도구 바꿈(byPromptVersion)이 없으면 이 버전의 것을 쓴다(v3 = v2 + 송금 규칙).
PROMPT_BASE = {
    "v3": "v2",
    "v3.1": "v3",
    "v3.2": "v3",
    "v4": "v3",
    "v4.1": "v4",
    "v4.2": "v4.1",
    "v4.3a": "v4.2",
    "v4.4": "v4.2",
}
# 프롬프트가 답에 쓰라고 주는 고정 번호. 그 run의 프롬프트에 있을 때만 근거 있는 숫자로 본다.
PROMPT_FIXED_NUMBERS = ("1345",)  # HiKorea 상담 번호(app/chat/prompts/visa_rules.md)
EXIT_INTERRUPTED = 130
EXIT_USAGE = 2

# USD per 1M tokens: input, output (thinking included), cache read, cache write.
# 2026-11-11 official Anthropic prices.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.10, 2.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}
# Upper-bound guess for the plan only: two LLM calls (tool call + answer), each with this
# much input and the full max_tokens of output. Real usage replaces it after the first call.
EST_INPUT_TOKENS_PER_LLM_CALL = 3000
EST_LLM_CALLS = 2
USAGE_KEYS = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")


def price_for(model: str | None) -> tuple[float, float, float, float]:
    if not model or model == FAKE_MODEL_ID:
        return (0.0, 0.0, 0.0, 0.0)
    for prefix, price in PRICES.items():
        if model.startswith(prefix):
            return price
    # Unknown model: assume the dearest known price so the budget guard stays safe.
    return PRICES["claude-sonnet-5-5"]


def cost_usd(usage: dict[str, Any] | None, model: str | None) -> float:
    if not usage:
        return 0.0
    p_in, p_out, p_read, p_write = price_for(model)
    return (
        float(
            usage.get("inputTokens", 0) * p_in
            + usage.get("outputTokens", 0) * p_out
            + usage.get("cacheReadInputTokens", 0) * p_read
            + usage.get("cacheCreationInputTokens", 0) * p_write
        )
        / 1_000_000
    )


def estimate_call_usd(model: str, max_tokens: int, fake: bool) -> float:
    if fake:
        return 0.0
    p_in, p_out, _, _ = price_for(model)
    per_llm = EST_INPUT_TOKENS_PER_LLM_CALL * p_in + max_tokens * p_out
    return EST_LLM_CALLS * per_llm / 1_000_000


def run_usage(done: dict[str, Any], metrics: dict[str, Any] | None) -> dict[str, Any] | None:
    """agent.done usage; for a run that ended in agent.error, the sum over agent.metrics calls."""
    if done.get("usage"):
        usage: dict[str, Any] = done["usage"]
        return usage
    calls = (metrics or {}).get("calls") or []
    if not calls:
        return None
    summed: dict[str, Any] = {k: sum(int(c.get(k) or 0) for c in calls) for k in USAGE_KEYS}
    summed["llmCalls"] = len(calls)
    return summed


def answering_model(done: dict[str, Any], metrics: dict[str, Any] | None) -> str | None:
    """The model that actually answered (done.modelId or the last call's model), if known."""
    models = [c["model"] for c in (metrics or {}).get("calls") or [] if c.get("model")]
    model: str | None = done.get("modelId") or (models[-1] if models else None)
    return model


def load_questions(path: Path = QUESTIONS) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def select_questions(
    spec: dict[str, Any], ids: str | None, subset: str | None, parser: argparse.ArgumentParser
) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = spec["questions"]
    if subset == "latency":
        keep = set(spec["latencySubset"])
        questions = [q for q in questions if q["id"] in keep]
    if ids:
        wanted = {part.strip() for part in ids.split(",") if part.strip()}
        unknown = sorted(wanted - {q["id"] for q in questions})
        if not wanted or unknown:
            parser.error(f"unknown or empty --questions: {unknown}")
        questions = [q for q in questions if q["id"] in wanted]
    return questions


def row_prompt_version(
    variant: dict[str, Any], done: dict[str, Any], metrics: dict[str, Any] | None
) -> str | None:
    """이 회차의 프롬프트 버전(설정값 v1·v1.1·v2). 서버가 보고한 값이 먼저, 없으면 variant 값."""
    reported = done.get("promptVersion") or ((metrics or {}).get("config") or {}).get(
        "promptVersion"
    )
    key = next((k for k, v in PROMPT_VERSIONS.items() if v == reported), None)
    return key or variant.get("promptVersion")


def expected_tools(question: dict[str, Any], prompt_version: str | None) -> list[str]:
    """문항의 기대 도구. byPromptVersion에 이 버전이 있으면 그 값(예: O01은 v2에서 점수 도구).

    없으면 그 버전의 바탕 버전(PROMPT_BASE, v3 → v2)의 값을 쓴다.
    """
    by_version = question.get("byPromptVersion") or {}
    version = prompt_version or ""
    override = by_version.get(version)
    while override is None and version in PROMPT_BASE:
        version = PROMPT_BASE[version]
        override = by_version.get(version)
    tools: list[str] = (override or question)["expectedTools"]
    return list(tools)


# --- 송금 재질의 세트 -------------------------------------------------------------------

# expectedAction → 1턴 기대 도구(toolMatch용. 판정은 remit_check가 따로 한다).
REMIT_EXPECTED_TOOLS = {
    "PROCEED_TO_CONFIRM": [GET_REMITTANCE_RECIPIENTS, QUOTE_REMITTANCE],
    "ASK_RECIPIENT": [GET_REMITTANCE_RECIPIENTS],
    "ASK_CURRENCY": [GET_REMITTANCE_RECIPIENTS],
    "ASK_AMOUNT": [GET_REMITTANCE_RECIPIENTS],
    "ASK_MORE_THAN_ONE": [GET_REMITTANCE_RECIPIENTS],
    "DECLINE_OR_GUIDE": [],
}
# 2턴째(followUpReply) 뒤 기대 받는 분·통화·금액. 질문 세트에는 없어서 followUpReply를 읽고 여기에
# 적었다(질문 세트는 고치지 않는다). 질문 세트에 적힌 값과 다르지 않은지는 테스트가 확인한다.
FOLLOW_UP_EXPECTED: dict[str, tuple[str, str, int]] = {
    "S10": ("MOTHER", "KRW", 500_000),  # to mama
    "S11": ("MOTHER", "KRW", 200_000),  # my mom
    "S12": ("SELF", "KRW", 300_000),  # my bpi
    "S13": ("MOTHER", "KRW", 250_000),  # mom
    "S14": ("MOTHER", "PHP", 20_000),  # pesos
    "S15": ("SELF", "PHP", 15_000),  # peso
    "S16": ("MOTHER", "KRW", 10_000),  # 10000 won only
    "S17": ("SELF", "KRW", 30_000),  # won
    "S18": ("MOTHER", "KRW", 500_000),  # 500k won
    "S19": ("SELF", "PHP", 5_000),  # 5000
    "S20": ("MOTHER", "KRW", 400_000),  # 400k
    "S21": ("MOTHER", "PHP", 20_000),  # to mama, 20000 pesos
    "S22": ("MOTHER", "KRW", 500_000),  # 500k won to mama
    "S23": ("MOTHER", "KRW", 500_000),  # mom 500k won
    "S24": ("MOTHER", "PHP", 15_000),  # my mom bdo, 15000 pesos
}
REMIT_SLOTS = ("expectedRecipient", "expectedCurrency", "expectedAmount")
SLOT_TOPIC = {
    "expectedRecipient": "recipient",
    "expectedCurrency": "currency",
    "expectedAmount": "amount",
}
ASK_TOPICS = {
    "ASK_RECIPIENT": {"recipient"},
    "ASK_CURRENCY": {"currency"},
    "ASK_AMOUNT": {"amount"},
}
# 되묻기 답변이 무엇을 묻는지: 받는 분 둘 중 고르기, 두 통화 중 고르기, 금액 묻기.
_ASK_RECIPIENT = re.compile(
    r"\b(who|whom|which (recipient|account|one|person)|recipient)\b", re.IGNORECASE
)
_KRW_WORD = re.compile(r"(?<![a-z])(krw|won|₩)\b", re.IGNORECASE)
_RECEIVE_WORD = re.compile(r"(?<![a-z])(php|pesos?|₱)\b", re.IGNORECASE)
_ASK_CURRENCY = re.compile(r"\bcurrency\b", re.IGNORECASE)
_ASK_AMOUNT = re.compile(r"\bhow much\b|\b(what|which) amount\b", re.IGNORECASE)


REMIT_FIELDS = {
    "id",
    "text",
    "category",
    "expectedAction",
    *REMIT_SLOTS,
    "followUpReply",
    "note",
}
REMIT_ACTIONS = {
    "clear": {"PROCEED_TO_CONFIRM"},
    "ambiguous": {"ASK_RECIPIENT", "ASK_CURRENCY", "ASK_AMOUNT", "ASK_MORE_THAN_ONE"},
    "out_of_scope": {"DECLINE_OR_GUIDE"},
}
ASKED_SLOT = {
    "ASK_RECIPIENT": "expectedRecipient",
    "ASK_CURRENCY": "expectedCurrency",
    "ASK_AMOUNT": "expectedAmount",
}


def remit_spec_errors(spec: Any) -> list[str]:
    """remit_questions_v1.json과 같은 스키마인지(문항 수·id 형식은 보지 않는다). 오류 문장 목록."""
    if not isinstance(spec, dict) or set(spec) != {"version", "questions"}:
        return ["top level must be {version, questions}"]
    questions = spec["questions"]
    if not isinstance(questions, list) or not questions:
        return ["questions must be a non-empty list"]
    errors: list[str] = []
    ids = [q.get("id") for q in questions if isinstance(q, dict)]
    if len(ids) != len(questions) or len(set(ids)) != len(ids):
        errors.append("every question needs a unique id")
    for i, q in enumerate(questions):
        name = q.get("id", f"#{i + 1}") if isinstance(q, dict) else f"#{i + 1}"
        if not isinstance(q, dict) or set(q) != REMIT_FIELDS:
            errors.append(f"{name}: fields must be {sorted(REMIT_FIELDS)}")
            continue
        action, category = q["expectedAction"], q["category"]
        amount = q["expectedAmount"]
        checks = [
            (isinstance(q["text"], str) and bool(q["text"].strip()), "empty text"),
            (action in REMIT_ACTIONS.get(category, set()), "category/expectedAction mismatch"),
            (q["expectedRecipient"] in ("MOTHER", "SELF", None), "bad expectedRecipient"),
            (q["expectedCurrency"] in ("KRW", "PHP", None), "bad expectedCurrency"),
            (
                amount is None or (type(amount) is int and amount > 0),
                "expectedAmount must be a positive integer or null",
            ),
            (
                bool(q["followUpReply"]) == (category == "ambiguous"),
                "followUpReply only (and always) for ambiguous questions",
            ),
            (
                action not in ASKED_SLOT or q[ASKED_SLOT.get(action, "")] is None,
                "the asked item must be null",
            ),
            (
                category != "clear" or all(q[slot] is not None for slot in REMIT_SLOTS),
                "clear questions need recipient, currency and amount",
            ),
            (
                action != "ASK_MORE_THAN_ONE" or sum(q[s] is None for s in REMIT_SLOTS) >= 2,
                "ASK_MORE_THAN_ONE needs at least two null items",
            ),
        ]
        errors += [f"{name}: {message}" for ok, message in checks if not ok]
    return errors


def is_remit_question(question: dict[str, Any]) -> bool:
    return "expectedAction" in question


def remit_question(question: dict[str, Any]) -> dict[str, Any]:
    """송금 문항에 측정 루프가 쓰는 필드(history, expectedTools)를 붙인 복사본."""
    return {
        **question,
        "history": [],
        "expectedTools": REMIT_EXPECTED_TOOLS[question["expectedAction"]],
    }


def remit_context(settings: Settings, user_id: int, llm_mode: str) -> dict[str, Any]:
    """송금 도구가 보는 받는 분·환율(근거 있는 숫자 확인과 recipientId → 관계 확인용)."""
    if llm_mode == "anthropic" and settings.spring_configured():
        client = SpringInternalClient(
            base_url=settings.spring_internal_api_url,
            api_key=settings.spring_internal_api_key.get_secret_value(),
            header=settings.internal_token_header,
        )
        try:
            recipients = client.remittance_recipients(user_id, timeout=10)["recipients"]
            currencies = sorted({r["currency"] for r in recipients})
            rates = [client.fx_rate(c, timeout=10) for c in currencies]
        finally:
            client.close()
        return {"recipients": recipients, "fxRates": rates}
    return fixture_remit_context()


def fixture_remit_context() -> dict[str, Any]:
    data = json.loads(REMITTANCE_FIXTURE.read_text(encoding="utf-8"))
    return {"recipients": data["recipients"], "fxRates": data["fxRates"]}


def row_remit_context(row: dict[str, Any]) -> dict[str, Any]:
    """이 행의 송금 도구 데이터. remitContext가 없는 행(이전 run)은 금융 도구 데이터로 추정한다.

    서버는 금융·송금 도구 모두 같은 조건(anthropic 모드 + Spring 설정)일 때만 Spring을 쓰므로,
    금융 데이터가 fixture와 같으면 송금 데이터도 fixture다. 다르면 알 수 없어서 비워 둔다.
    """
    saved: dict[str, Any] | None = row.get("remitContext")
    if saved:
        return saved
    if row.get("toolResult") == fixture_context():
        return fixture_remit_context()
    return {}


def turn_facts(
    tools: list[dict[str, Any]],
    blocks: list[dict[str, Any]] | None,
    answer: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    """한 턴이 한 일: 견적·카드로 진행했는지, 무엇을 물었는지, 견적 인자(관계·통화·금액)."""
    quotes = [t for t in tools if t.get("toolName") == QUOTE_REMITTANCE]
    cards = [b for b in blocks or [] if b.get("type") == "REMITTANCE_CONFIRM"]
    relationship = {r["recipientId"]: r["relationship"] for r in context.get("recipients", [])}
    quoted = None
    succeeded = [q for q in quotes if q.get("status") == "SUCCEEDED"] or quotes
    if succeeded:
        args = succeeded[-1].get("arguments") or {}
        quoted = {
            "recipient": relationship.get(args.get("recipientId"), "UNKNOWN"),
            "currency": args.get("currency"),
            "amount": args.get("amount"),
        }
    proceeded = bool(quotes or cards)
    topics: list[str] = []
    if not proceeded and "?" in answer:
        recipients = context.get("recipients", [])
        named = sum(
            1
            for r in recipients
            if any(word.lower() in answer.lower() for word in (r["nameEn"], r["bankName"]) if word)
        )
        if _ASK_RECIPIENT.search(answer) or named >= 2:
            topics.append("recipient")
        if _ASK_CURRENCY.search(answer) or (
            _KRW_WORD.search(answer) and _RECEIVE_WORD.search(answer)
        ):
            topics.append("currency")
        if _ASK_AMOUNT.search(answer):
            topics.append("amount")
    return {
        "proceeded": proceeded,
        "card": bool(cards),
        "quote": quoted,
        "asked": not proceeded and "?" in answer,
        "askedAbout": topics,
    }


def _guess_errors(quoted: dict[str, Any] | None, expected: dict[str, Any]) -> list[str]:
    """견적 인자가 기대값과 다른 항목(기대값이 없는 항목은 보지 않는다)."""
    if quoted is None:
        return []
    return [
        key
        for key in ("recipient", "currency", "amount")
        if expected.get(key) is not None and quoted.get(key) != expected[key]
    ]


# --- 환율 단위 판정 ---------------------------------------------------------------------
# 도구 결과의 환율(appliedRate.rate: `unit` 외화 = `rate` KRW)이 답변에 나오면 단위·방향을 본다.
# correct: "22.32 KRW per PHP", "22.32 won per peso", "1 PHP = 22.32 KRW", "1 PHP costs 22.32 KRW"
# wrong: "22.32 PHP per ...", "... per 1,000 KRW", "per KRW", "1 KRW = 22.32" (방향이 거꾸로)
# no_unit: 숫자만("the applied rate of 22.32."), unclear: 위 어디에도 안 맞음(사람 확인)
# 한 답변(두 턴 합)에 correct와 wrong이 섞이면 wrong이다.
_KRW_UNIT = r"(?:KRW|won|₩|원)"
_FOREIGN_NAMES = {"PHP": r"pesos?|₱", "VND": r"dong|₫"}
_RATE_PER = r"(?:per|/|for each|for every|for one|for a|for)"
_RATE_IS = (
    r"(?:=|is equal to|equals|is worth|is about|is around|costs about|costs|is|buys|gets you)"
)


def _foreign_unit(currencies: set[str]) -> str:
    names = [code for code in sorted(currencies) if code != "KRW"]
    names += [_FOREIGN_NAMES[c] for c in sorted(currencies) if c in _FOREIGN_NAMES]
    return "(?:" + "|".join(names or ["PHP"]) + ")"


def remit_rates(row: dict[str, Any]) -> dict[Decimal, str]:
    """이 행에서 도구가 준 환율 값 → 통화(받는 분·환율 데이터와 확인 카드)."""
    follow = row.get("followUp") or {}
    rates: dict[Decimal, str] = {}
    items = list(row_remit_context(row).get("fxRates") or [])
    for block in [*(row.get("blocks") or []), *(follow.get("blocks") or [])]:
        if block.get("type") == "REMITTANCE_CONFIRM" and block.get("appliedRate"):
            items.append(block["appliedRate"])
    for item in items:
        if isinstance(item, dict) and item.get("rate") is not None:
            rates[Decimal(str(item["rate"])).normalize()] = str(item.get("currency") or "PHP")
    return rates


def _sentence_around(text: str, start: int, end: int) -> tuple[str, str]:
    """숫자 앞(문장 처음부터)과 뒤(문장 끝까지). 숫자 안의 점은 문장 끝으로 보지 않는다."""
    before_cut = max((m.end() for m in re.finditer(r"[.!?](?:\s+|$)|\n", text[:start])), default=0)
    after = text[end:]
    stop = re.search(r"[.!?](?:\s|$)|\n", after)
    return text[before_cut:start], after[: stop.start() if stop else len(after)]


def rate_mention_verdict(before: str, after: str, foreign: str) -> str:
    flags = re.IGNORECASE
    lead = r"(?:\d[\d,]*\s*|one\s+|a\s+|each\s+|every\s+)?"
    if re.match(rf"\s*{foreign}\s*{_RATE_PER}", after, flags) or re.match(
        rf"\s*(?:{_KRW_UNIT}\s*)?per\s*(?:\d[\d,]*\s*)?{_KRW_UNIT}", after, flags
    ):
        return "wrong"
    if re.search(rf"{lead}{_KRW_UNIT}\s*{_RATE_IS}\s*(?:about\s*|around\s*)?$", before, flags):
        return "wrong"
    if re.match(
        rf"\s*{_KRW_UNIT}\s*{_RATE_PER}\s*(?:1\s*|one\s*|each\s*|every\s*|a\s*)?{foreign}",
        after,
        flags,
    ):
        return "correct"
    foreign_first = re.search(
        rf"{lead}{foreign}\s*{_RATE_IS}\s*(?:about\s*|around\s*)?({_KRW_UNIT}\s*)?$", before, flags
    )
    if foreign_first and (foreign_first.group(1) or re.match(rf"\s*{_KRW_UNIT}", after, flags)):
        return "correct"
    if not re.match(rf"\s*(?:{_KRW_UNIT}|{foreign})", after, flags) and not re.search(
        rf"(?:{_KRW_UNIT}|{foreign})\s*$", before, flags
    ):
        return "no_unit"
    return "unclear"


def rate_unit_check(row: dict[str, Any], answer: str) -> dict[str, Any] | None:
    """답변 안의 환율 언급 판정. 도구가 준 환율 값이 답변에 없으면 None."""
    rates = remit_rates(row)
    mentions: list[dict[str, str]] = []
    foreign = _foreign_unit(set(rates.values()) | {"PHP"})
    for value in rates:
        number = re.escape(format(value, "f"))
        for m in re.finditer(rf"(?<![\d,.]){number}(?![\d])", answer):
            before, after = _sentence_around(answer, m.start(), m.end())
            mentions.append(
                {
                    "sentence": (before + m.group() + after).strip(),
                    "verdict": rate_mention_verdict(before, after, foreign),
                }
            )
    if not mentions:
        return None
    verdicts = {m["verdict"] for m in mentions}
    if "wrong" in verdicts:
        verdict = "wrong"
    elif "unclear" in verdicts:
        verdict = "unclear"
    elif "correct" in verdicts:
        verdict = "correct"
    else:
        verdict = "no_unit"
    return {"verdict": verdict, "mentions": mentions}


def remit_check(row: dict[str, Any]) -> dict[str, Any]:
    """송금 문항 판정. 규칙으로 정할 수 없는 것은 needsReview로 남긴다.

    outcome: correct | missed_clarification | unnecessary_question | wrong_guess |
    out_of_scope_proceeded | declined | not_completed | asked_other | error | unclear
    """
    action = row["expectedAction"]
    context = row.get("remitContext") or {}
    first = turn_facts(row.get("tools", []), row.get("blocks"), row.get("answer") or "", context)
    follow = row.get("followUp")
    second = (
        turn_facts(
            follow.get("tools", []), follow.get("blocks"), follow.get("answer") or "", context
        )
        if follow
        else None
    )
    stated = {
        "recipient": row.get("expectedRecipient"),
        "currency": row.get("expectedCurrency"),
        "amount": row.get("expectedAmount"),
    }
    result: dict[str, Any] = {
        "outcome": "unclear",
        "turns": None,
        "guessErrors": [],
        "askedAbout": first["askedAbout"],
        "askedMany": len(first["askedAbout"]) > 1,
        "needsReview": False,
        "reason": "",
        "first": first,
        "second": second,
    }

    def done(outcome: str, *, turns: int | None = None, review: str = "") -> dict[str, Any]:
        result.update(outcome=outcome, turns=turns, reason=review)
        result["needsReview"] = bool(review) or result["askedMany"]
        if result["askedMany"] and not review:
            result["reason"] = "한 번에 둘 이상을 물음"
        return result

    if row.get("status") != "done":
        return done("error", review="1턴 오류")
    if action == "DECLINE_OR_GUIDE":
        if first["proceeded"]:
            if any(v is not None for v in stated.values()):
                # 예: S27 "묻지 말고 보내" — 카드를 낼 수는 있지만 즉시 송금 불가를 말했는지 본다.
                result["guessErrors"] = _guess_errors(first["quote"], stated)
                return done("out_of_scope_proceeded", review="카드를 냈다: 안내 문구 확인")
            return done("out_of_scope_proceeded")
        return done("declined", review="범위 밖 안내 문구 확인")
    if action == "PROCEED_TO_CONFIRM":
        if first["proceeded"]:
            result["guessErrors"] = _guess_errors(first["quote"], stated)
            if result["guessErrors"]:
                return done("wrong_guess", turns=1)
            return done("correct", turns=1)
        if first["asked"]:
            return done("unnecessary_question", review="" if first["askedAbout"] else "질문 확인")
        return done("unclear", review="진행도 질문도 아님")
    # 애매한 문항
    if first["proceeded"]:
        result["guessErrors"] = _guess_errors(first["quote"], stated)
        return done("missed_clarification", turns=1)
    expected_topics = ASK_TOPICS.get(action) or {
        SLOT_TOPIC[slot] for slot in REMIT_SLOTS if row.get(slot) is None
    }
    asked = set(first["askedAbout"])
    review = ""
    if not first["asked"]:
        review = "1턴에 질문이 없음"
    elif not asked:
        review = "무엇을 물었는지 규칙으로 못 정함"
    elif not asked & expected_topics:
        result["outcome"] = "asked_other"
        review = "다른 항목을 물음"
    elif not asked <= expected_topics:
        review = "기대 항목과 다른 항목을 같이 물음"
    if second is None:
        if review == "다른 항목을 물음":
            return done("asked_other", review=review)
        return done("unclear", review=review or "2턴이 없음")
    if second["proceeded"]:
        # 고정 세트는 FOLLOW_UP_EXPECTED, 외부 질문 파일은 2턴 뒤 값을 모른다(행에 null로 남김).
        # followUpExpected가 없는 이전 행은 고정 세트로 잰 것이다.
        final = (
            row["followUpExpected"]
            if "followUpExpected" in row
            else FOLLOW_UP_EXPECTED.get(row["questionId"])
        )
        expected = (
            dict(zip(("recipient", "currency", "amount"), final, strict=True)) if final else stated
        )
        result["guessErrors"] = _guess_errors(second["quote"], expected)
        if not final and not review:
            unknown = [k for k in ("recipient", "currency", "amount") if stated[k] is None]
            if unknown:
                review = f"2턴 값({', '.join(unknown)})은 사람 확인"
        if review == "다른 항목을 물음":
            return done("asked_other", turns=2, review=review)
        if result["guessErrors"]:
            return done("wrong_guess", turns=2, review=review)
        return done("correct", turns=2, review=review)
    return done("not_completed", review=review or "2턴에도 진행하지 않음")


def load_variants(path: Path | None, settings: Settings) -> list[dict[str, Any]]:
    if path is None:
        return [
            {
                "name": "server-default",
                "model": settings.chat_model_id,
                "thinking": settings.chat_thinking,
                "effort": settings.chat_effort,
                "maxTokens": settings.chat_max_tokens,
                "judgmentFacts": settings.chat_judgment_facts,
                "promptVersion": settings.chat_prompt_version,
                "override": False,
            }
        ]
    variants: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    names = [v.get("name") for v in variants]
    if not variants or any(not n for n in names) or len(set(names)) != len(names):
        raise ValueError("--variants needs a non-empty list with unique names")
    for v in variants:
        v.setdefault("model", settings.chat_model_id)
        v.setdefault("maxTokens", settings.chat_max_tokens)
        v.setdefault("judgmentFacts", settings.chat_judgment_facts)
        v.setdefault("promptVersion", settings.chat_prompt_version)
        v["override"] = True
    return variants


def config_header(variant: dict[str, Any]) -> str | None:
    if not variant.get("override"):
        return None
    keys = ("model", "thinking", "effort", "maxTokens", "judgmentFacts", "promptVersion")
    return json.dumps({k: variant[k] for k in keys if variant.get(k) is not None})


def _summary(context: dict[str, Any]) -> dict[str, Any]:
    if context.get("latestAnalysis") is None:
        return {"analysis": None}
    return summarize(context)


def fixture_context() -> dict[str, Any]:
    """The demo fixture summary the expected conclusions and reference values are worked out on."""
    return _summary(json.loads(FIXTURE.read_text(encoding="utf-8")))


def tool_context(settings: Settings, user_id: int, llm_mode: str) -> dict[str, Any]:
    """The INT-06 data the server's tool sees, summarized the same way (for number checks)."""
    if llm_mode == "anthropic" and settings.spring_configured():
        client = SpringInternalClient(
            base_url=settings.spring_internal_api_url,
            api_key=settings.spring_internal_api_key.get_secret_value(),
            header=settings.internal_token_header,
        )
        try:
            context = client.finance_context(user_id, timeout=10)
        finally:
            client.close()
        return _summary(context)
    return fixture_context()


def adopt_server_prompt_version(
    variant: dict[str, Any], metrics: dict[str, Any] | None
) -> str | None:
    """server-default는 서버가 실제로 쓴 프롬프트 버전을 따른다(첫 응답에서 한 번만).

    server-default는 서버에 설정을 보내지 않으므로, 이 스크립트 프로세스의 설정(환경 변수·ai/.env)이
    서버의 CHAT_PROMPT_VERSION과 다를 수 있다(#88 v4.3a 스모크: 서버 v4.3a, 스크립트 v4.2로 멈춤).
    첫 응답이 보고한 버전으로 바꾸고 원래 값은 localPromptVersion에 남긴다. 그 뒤 응답은
    config_mismatch가 그 버전과 엄격히 비교한다(측정 중 서버가 바뀌면 멈춘다).
    바꿨으면 원래 값을 돌려준다.
    --variants(override)는 서버에 버전을 보내므로 따르지 않는다.
    """
    if variant.get("override") or variant.get("promptVersionFromServer"):
        return None
    reported = ((metrics or {}).get("config") or {}).get("promptVersion")
    key = next((k for k, v in PROMPT_VERSIONS.items() if v == reported), None)
    if key is None:
        return None
    variant["promptVersionFromServer"] = True
    previous = variant.get("promptVersion")
    if key == previous:
        return None
    variant["promptVersion"] = key
    variant["localPromptVersion"] = previous
    return str(previous)


def config_mismatch(
    variant: dict[str, Any], answered_by: str | None, metrics: dict[str, Any] | None, fake: bool
) -> list[str]:
    """What the server ran that differs from the variant (empty when it matches or is unknown)."""
    config = (metrics or {}).get("config") or {}
    problems = []
    actual = [config.get("model")] + ([] if fake else [answered_by])
    for model in dict.fromkeys(m for m in actual if m):
        if not model.startswith(variant["model"]):
            problems.append(f"model {model} != {variant['model']}")
    facts = config.get("judgmentFacts")
    if facts is not None and facts != bool(variant.get("judgmentFacts")):
        problems.append(f"judgmentFacts {facts} != {bool(variant.get('judgmentFacts'))}")
    wanted = PROMPT_VERSIONS.get(str(variant.get("promptVersion")))
    version = config.get("promptVersion")
    if version is not None and wanted is not None and version != wanted:
        problems.append(f"promptVersion {version} != {wanted}")
    return problems


def iter_sse(lines: Iterator[str]) -> Iterator[tuple[str, dict[str, Any]]]:
    name: str | None = None
    data: list[str] = []
    for line in lines:
        if line == "":
            if name and data:
                yield name, json.loads("\n".join(data))
            name, data = None, []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())


def stream_call(
    client: httpx.Client, url: str, body: dict[str, Any], headers: dict[str, str], prefix: str
) -> dict[str, Any]:
    """POST and read the SSE stream. Times are monotonic ms from the request start."""
    started = time.perf_counter()

    def ms() -> int:
        return round((time.perf_counter() - started) * 1000)

    result: dict[str, Any] = {"events": [], "timings": {}, "deltas": [], "deltaMs": [], "tools": []}
    with client.stream("POST", url, json=body, headers=headers, timeout=180) as response:
        result["httpStatus"] = response.status_code
        if response.status_code != 200:
            response.read()
            result["status"] = "http_error"
            try:
                result["errorCode"] = response.json().get("code")
            except ValueError:
                result["errorCode"] = None
            result["timings"]["doneMs"] = ms()
            return result

        def lines() -> Iterator[str]:
            for line in response.iter_lines():
                result["timings"].setdefault("firstByteMs", ms())
                yield line

        for name, data in iter_sse(lines()):
            result["events"].append(name)
            if name == f"{prefix}.delta":
                at = ms()
                result["timings"].setdefault("firstDeltaMs", at)
                result["deltas"].append(data.get("text", ""))
                result["deltaMs"].append(at)  # arrival per delta, to replay the stream later
            elif name == f"{prefix}.tool_call":
                result["tools"].append(data)
            elif name == f"{prefix}.metrics":
                result["metrics"] = data
            elif name == f"{prefix}.done":
                result["timings"]["doneMs"] = ms()
                result["done"] = data
                result["status"] = "done"
            elif name == f"{prefix}.error":
                result["timings"]["doneMs"] = ms()
                result["status"] = "error"
                result["errorCode"] = data.get("code")
    result.setdefault("status", "incomplete")
    return result


def call_ai(
    client: httpx.Client,
    base_url: str,
    token: str,
    question: dict[str, Any],
    variant: dict[str, Any],
    user_id: int,
    language: str = "en",
) -> dict[str, Any]:
    run_id = "run_" + uuid.uuid4().hex
    body = {
        "runId": run_id,
        "userId": user_id,
        "sessionId": 1,
        "language": language,
        "history": question.get("history", []),
        "message": question["text"],
    }
    headers = {
        "X-Internal-Token": token,
        "X-Chat-Metrics": "1",
        "X-Request-Id": f"eval_{run_id[4:24]}",
    }
    header = config_header(variant)
    if header:
        headers["X-Chat-Config"] = header
    if question.get("productProfileHeader"):
        # 상품 문항(#65): 질문 파일 profiles의 값(CHAT_ALLOW_CONFIG_OVERRIDE=true일 때만 쓰인다).
        headers["X-Chat-Product-Profile"] = question["productProfileHeader"]
    url = base_url.rstrip("/") + "/internal/v1/agent/chat/stream"
    return stream_call(client, url, body, headers, "agent")


def run_follow_up(
    client: httpx.Client,
    base_url: str,
    token: str,
    question: dict[str, Any],
    first_answer: str,
    variant: dict[str, Any],
    user_id: int,
    language: str = "en",
) -> dict[str, Any]:
    """애매한 송금 문항의 2턴째: 1턴 질문·답변을 history로 보내고 followUpReply를 묻는다."""
    second = {
        "text": question["followUpReply"],
        "history": [
            {"role": "USER", "content": question["text"]},
            {"role": "ASSISTANT", "content": first_answer},
        ],
    }
    result = call_ai(client, base_url, token, second, variant, user_id, language)
    done = result.get("done") or {}
    metrics = result.get("metrics")
    model = answering_model(done, metrics) or (metrics or {}).get("config", {}).get("model")
    usage = run_usage(done, metrics)
    return {
        "message": question["followUpReply"],
        "status": result.get("status"),
        "errorCode": result.get("errorCode"),
        "answer": "".join(result["deltas"]),
        "tools": result["tools"],
        "blocks": done.get("blocks"),
        "remitTools": remit_tool_results(metrics),
        "usage": usage,
        "model": model,
        "costUsd": round(cost_usd(usage, model), 6),
        "timings": result["timings"],
    }


def call_spring(
    client: httpx.Client, base_url: str, jwt: str, question: dict[str, Any]
) -> dict[str, Any]:
    """CHT-01 new session, then CHT-04. History cannot be injected here (it lives in MySQL)."""
    auth = {"Authorization": f"Bearer {jwt}"}
    base = base_url.rstrip("/")
    created = client.post(f"{base}/api/v1/chat/sessions", json={"title": None}, headers=auth)
    created.raise_for_status()
    session_id = created.json()["id"]
    body = {"clientMessageId": str(uuid.uuid4()), "content": question["text"]}
    url = f"{base}/api/v1/chat/sessions/{session_id}/messages/stream"
    return stream_call(client, url, body, {**auth, "Accept": "text/event-stream"}, "chat")


def reference_matches(answer: str, ref: dict[str, Any]) -> list[str]:
    """Answer numbers that state the reference value (number_check scan, same rounding rules).

    KRW: exact, or rounded half-up to 1,000 / 10,000 won ("about 8.1 million").
    percent: the one-decimal value or the value rounded half-up to a whole number (29.8, 30).
    """
    value = Decimal(str(ref["value"]))
    if ref["unit"] == "percent":
        accepted = {value, value.quantize(Decimal(1), rounding=ROUND_HALF_UP)}
        kinds = {"percent"}
    else:
        accepted = {value} | rounded_values({value})
        kinds = {"amount", "number"}
    return [
        text for text, found, kind, _ in scan_numbers(answer) if kind in kinds and found in accepted
    ]


def reference_check(
    answer: str, refs: list[dict[str, Any]], unsupported: list[str]
) -> dict[str, Any]:
    """reference: every reference value is in the answer; wrong_number: not, but the answer has
    numbers the tool result does not support; no_value: neither (e.g. it declined)."""
    matched = {ref["formula"]: reference_matches(answer, ref) for ref in refs}
    if all(matched.values()):
        verdict = "reference"
    elif unsupported:
        verdict = "wrong_number"
    else:
        verdict = "no_value"
    return {"verdict": verdict, "matched": matched, "unsupported": unsupported}


def prompt_fixed_numbers(row: dict[str, Any]) -> list[str]:
    """이 행의 프롬프트(서버가 보고한 promptVersion)에 들어 있는 고정 번호. 모르면 없음."""
    version_id = ((row.get("metrics") or {}).get("config") or {}).get("promptVersion")
    key = next((k for k, v in PROMPT_VERSIONS.items() if v == version_id), None)
    if key is None:
        return []
    prompt = system_prompt("en", key)
    return [number for number in PROMPT_FIXED_NUMBERS if number in prompt]


def refresh_expected_tools(
    conditions: dict[str, Any], rows: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """--rescore: 기대 도구만 지금 질문 세트에서 다시 읽는다(측정 뒤 고친 byPromptVersion 반영).

    기대 결론·참조값은 행에 저장된 값(그 run의 채점 기준)을 그대로 쓴다. 질문 세트 파일을 모르거나
    문항이 없어진 행은 저장된 기대 도구를 둔다. 돌려주는 값은 읽은 질문 세트(파일·버전).
    """
    question_set = conditions.get("questionSet") or {}
    name = question_set.get("file")
    if question_set.get("external"):
        return None  # --remit-questions로 잰 run: 레포 밖 파일은 다시 읽지 않는다
    path = next((p for p in QUESTION_SETS.values() if p.name == name), None)
    if path is None:
        return None
    spec = load_questions(path)
    by_id = {q["id"]: q for q in spec["questions"]}
    for row in rows:
        question = by_id.get(row["questionId"])
        if question is None:
            continue
        if is_remit_question(question):
            question = remit_question(question)
        row["expectedTools"] = expected_tools(question, row.get("promptVersion"))
        outcome = question_outcome(spec, question)
        if "expectedOutcome" not in row and outcome is not None:
            # 상품 채점(#65) 전에 저장된 행: 기대 결과를 질문 세트에서 채운다(저장된 값은 그대로).
            row["expectedOutcome"] = outcome
            row.update(product_fields(spec, question))
    return {"file": name, "version": spec.get("version")}


def question_outcome(spec: dict[str, Any], question: dict[str, Any]) -> dict[str, Any] | None:
    """상품 문항(#65)의 기대 결과. 상품 문항이 아니면 None.

    질문 세트에 expectedOutcome이 없으면(hold-out) expectedArgs가 null인 문항은 범위 밖,
    그 밖은 기대 인자·프로필로 추천기를 돌려 추천/기권(이유 코드)을 정한다.
    """
    if question.get("expectedOutcome") is not None:
        outcome: dict[str, Any] = question["expectedOutcome"]
        return outcome
    if "expectedArgs" not in question:
        return None
    if question["expectedArgs"] is None:
        return {"type": "out_of_scope"}
    profile = (spec.get("profiles") or {}).get(question.get("profileId") or "")
    if not profile or not spec.get("asOf"):
        return None
    gold, reason = _product_check.expected_gold(question["expectedArgs"], profile, spec["asOf"])
    return {"type": "recommend"} if gold else {"type": "abstain", "reason": reason}


def product_fields(spec: dict[str, Any], question: dict[str, Any]) -> dict[str, Any]:
    """상품 문항(#65) 채점에 쓰는 기대 값: 인자, 통과 집합(gold), 프로필, 기준일."""
    if question_outcome(spec, question) is None:
        return {}
    profile_id = question.get("profileId")
    return {
        "expectedArgs": question.get("expectedArgs"),
        "gold": question.get("gold"),
        "productProfile": (spec.get("profiles") or {}).get(profile_id) if profile_id else None,
        "productAsOf": spec.get("asOf"),
    }


def with_profile_header(spec: dict[str, Any], question: dict[str, Any]) -> dict[str, Any]:
    """상품 문항에 서버로 보낼 프로필 값(JSON, 측정 전용 헤더)을 붙인 복사본.

    질문 파일의 profiles가 기준이다. 서버는 이 값으로 추천하고,
    채점도 같은 값으로 기대 결과를 계산한다.
    """
    profile = (spec.get("profiles") or {}).get(question.get("profileId") or "")
    if not profile:
        return question
    values = {
        "visaTypeCode": profile.get("visa"),
        "stayExpiresOn": profile.get("stayExpiry"),
        "age": profile.get("age"),
        "asOf": spec.get("asOf"),
        "spareKrw": profile.get("spareKrw"),
    }
    return {**question, "productProfileHeader": json.dumps(values, separators=(",", ":"))}


def product_tool_result(metrics: dict[str, Any] | None) -> dict[str, Any] | None:
    """agent.metrics의 상품 도구 측정 값(첫 호출)."""
    for tool in (metrics or {}).get("tools") or []:
        if tool.get("toolName") == _product_check.PRODUCT_TOOL:
            return dict(tool)
    return None


def policy_tool_results(metrics: dict[str, Any] | None) -> list[dict[str, Any]]:
    """agent.metrics의 금융 정책 검색 도구 측정 값(호출마다, #88). 결과 본문은 근거 숫자다."""
    return [
        dict(tool)
        for tool in (metrics or {}).get("tools") or []
        if tool.get("toolName") == "search_finance_policy"
    ]


def remit_tool_results(metrics: dict[str, Any] | None) -> list[dict[str, Any]]:
    """agent.metrics의 송금 견적 도구(quote_remittance) 측정 값(호출마다, #105).

    인자와 LLM에 준 결과(content: 수수료·USD 송금 환율·출금 총액 등)가 있다. 숫자 검사 근거다.
    """
    return [
        dict(tool)
        for tool in (metrics or {}).get("tools") or []
        if tool.get("toolName") == QUOTE_REMITTANCE
    ]


def fx_tool_results(metrics: dict[str, Any] | None) -> list[dict[str, Any]]:
    """agent.metrics의 환율 진단 도구(fx_rate_summary, #91) 측정 값(호출마다).

    인자와 LLM에 준 결과(content)가 있다. 숫자 검사가 content를 근거로 쓴다.
    """
    return [
        dict(tool)
        for tool in (metrics or {}).get("tools") or []
        if tool.get("toolName") == FX_RATE_SUMMARY
    ]


def row_gold(row: dict[str, Any]) -> list[str]:
    """행의 통과 집합. 질문 세트에 gold가 없으면(hold-out) 기대 인자·프로필로 계산한다."""
    if row.get("gold") is not None:
        return list(row["gold"])
    if row.get("expectedArgs") is None or not row.get("productProfile"):
        return []
    gold, _ = _product_check.expected_gold(
        row["expectedArgs"], row["productProfile"], row["productAsOf"]
    )
    return gold


def score(row: dict[str, Any]) -> None:
    """Number check, reference check and expected-tool match from what the row already holds."""
    answer = row.get("answer") or ""
    tool_results = [row["toolResult"]]
    if row.get("judgmentFacts"):
        # Same function as the server, on the same summary and this turn's message.
        facts = judgment_facts(row["toolResult"], row["contextTexts"][0])
        if facts is not None:
            tool_results.append(facts)
    follow = row.get("followUp") or {}
    if row.get("expectedAction"):
        # 송금 문항: 두 턴의 답변을 함께 본다.
        answer = "\n\n".join(a for a in (answer, follow.get("answer") or "") if a)
    called_remittance = any(
        t.get("toolName") in REMITTANCE_TOOL_NAMES
        for t in [*row.get("tools", []), *follow.get("tools", [])]
    )
    if row.get("expectedAction") or called_remittance:
        # 송금 도구 결과도 근거다. 받는 분·환율(get_remittance_recipients·get_fx_rate)은
        # remitContext, 견적(quote_remittance)은 그 결과로 만든 확인 카드(blocks)와 숫자가 같다.
        # 환율 진단 카드(FX_SUMMARY_CARD, #95)는 뺀다: LLM이 받지 않은 값(sendRate 등)이 있고,
        # 환율 진단 근거는 아래 fx_results(LLM에 준 값)가 따로 맡는다.
        tool_results += [
            row_remit_context(row),
            *(
                b
                for b in [*(row.get("blocks") or []), *(follow.get("blocks") or [])]
                if b.get("type") != FX_SUMMARY_CARD
            ),
            # 견적이 LLM에 준 값(#105): 카드에 없는 USD 금액·1년 송금 횟수 등도 근거다.
            *(
                t["content"]
                for t in [*(row.get("remitTools") or []), *(follow.get("remitTools") or [])]
                if t.get("content")
            ),
        ]
    product_tool = row.get("productTool")
    if product_tool and product_tool.get("content"):
        # 상품 도구가 LLM에 준 값(기간·금액 범위·여유자금·남은 체류 개월)도 근거다.
        tool_results.append(product_tool["content"])
    for policy_tool in row.get("policyTools") or []:
        # 금융 정책 검색 결과(조문 원문)도 근거다(#88, v4.2).
        if policy_tool.get("content"):
            tool_results.append(policy_tool["content"])
    # 환율 진단 도구가 LLM에 준 값(현재 값·기간 평균·차이·영업일 수·기준일)도 근거다.
    # 이 결과만 음수의 절댓값도 근거로 본다("2.6% lower" ← differencePct -2.6).
    fx_results = [t.get("content") for t in row.get("fxTools") or []]
    check = check_answer(
        answer,
        tool_results,
        [*row["contextTexts"], *prompt_fixed_numbers(row)],
        fx_results=fx_results,
    )
    row["numberCheck"] = check.to_json()
    if fx_results or row.get("category") == "fx":
        # 환율 진단 답변(#91): 기간 평균과의 방향, 예측·권유 문장(scripts/_fx_check.py).
        row["fxCheck"] = {
            "direction": _fx_check.direction_check(answer, fx_results),
            "advice": _fx_check.advice_sentences(answer),
        }
    else:
        row.pop("fxCheck", None)
    # 금지 표현(실시간·환율 예측·근거 없는 우대율, #101)은 모든 답변에서 본다.
    row["forbidden"] = _fx_check.forbidden_phrases(answer)
    # 답변 언어(#106): 사용자 언어와 다른 글자(한글 혼입 등). 언어가 없는 옛 행은 en(당시 고정값).
    row["languageCheck"] = _lang_check.language_check(
        answer, row.get("language") or "en", row["contextTexts"]
    )
    if row.get("wantsImprovementCheck"):
        # LE03 보조 지표(#106): 개선 방안(한국어 능력·점수 증가·필요 서류) 언급.
        row["improvementCheck"] = _lang_check.improvement_check(answer, row.get("language") or "en")
    if row.get("referenceValues"):
        row["referenceCheck"] = reference_check(answer, row["referenceValues"], check.unsupported)
    called = sorted({t.get("toolName") for t in row.get("tools", [])})
    row["toolMatch"] = called == sorted(set(row["expectedTools"]))
    if row.get("expectedOutcome") is not None:
        # 상품 문항(product_questions_v1.json): 거절·상품명·금리 언급(scripts/_product_check.py).
        row["productCheck"] = _product_check.product_check(
            row.get("answer") or "",
            [t.get("toolName") for t in row.get("tools", [])],
            out_of_scope=row["expectedOutcome"].get("type") == "out_of_scope",
        )
        # 상품 도구가 붙는 프롬프트 버전(v4)의 행: 범위 밖 문항의 '부르지 말아야 할 때 호출'도 본다.
        if row.get("promptVersion") in PRODUCT_PROMPT_VERSIONS or product_tool is not None:
            # v4(상품 도구가 붙은 버전): 인자·끝까지·도구에 없는 상품명(결과 문서 3절).
            row["productToolCheck"] = _product_check.tool_check(
                product_tool,
                expected_args=row.get("expectedArgs"),
                expected_outcome=row["expectedOutcome"],
                gold=row_gold(row),
                answer_names=row["productCheck"]["catalogNames"],
            )
        else:
            row.pop("productToolCheck", None)
    if row.get("expectedAction") or called_remittance:
        # 두 턴의 답변을 함께 본다(송금 문항이 아니면 1턴뿐).
        full = "\n\n".join(a for a in (row.get("answer") or "", follow.get("answer") or "") if a)
        row["rateUnit"] = rate_unit_check(row, full)
    else:
        row.pop("rateUnit", None)
    if row.get("expectedAction"):
        row["remitCheck"] = remit = remit_check(row)
        if (row.get("rateUnit") or {}).get("verdict") == "unclear":
            remit["needsReview"] = True
            remit["reason"] = "; ".join(r for r in (remit["reason"], "환율 단위 확인") if r)


def percentile_95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(math.ceil(0.95 * len(ordered)), 1) - 1]


def _stat(values: list[float]) -> str:
    if not values:
        return "-"
    text = f"{statistics.median(values):.0f} ms ({min(values):.0f}~{max(values):.0f})"
    if len(values) >= 20:
        text += f", p95 {percentile_95(values):.0f}"
    return text


def measured_rows(rows: list[dict[str, Any]], variant: str) -> list[dict[str, Any]]:
    """The rows a variant's summary counts: every non-warmup call, errors included."""
    return [r for r in rows if r["variant"] == variant and not r["warmup"]]


def is_graded(row: dict[str, Any]) -> bool:
    """Graded by a person; a right answer may calculate a number the tool result lacks."""
    return bool(row.get("expectedConclusion"))


def unsupported_detail(rows: list[dict[str, Any]]) -> str:
    return "; ".join(
        f"{r['questionId']}: {', '.join(r['numberCheck']['unsupported'])}" for r in rows
    )


def render_summary(
    conditions: dict[str, Any], rows: list[dict[str, Any]], *, answers: bool = True
) -> str:
    """Summary markdown. answers=False leaves out the judgment answers (for stdout)."""
    out = [
        "# 챗봇 측정 요약",
        "",
        "## 측정 조건",
        "",
        f"- 대상: {conditions['target']}, 모드(--llm-mode): {conditions['mode']}",
        f"- 질문 세트: {(conditions.get('questionSet') or {}).get('file', '-')} "
        f"({(conditions.get('questionSet') or {}).get('version', '-')})"
        + (
            f", 질문 파일 {conditions['questionSet']['path']} "
            f"(문항 {conditions['questionSet'].get('count')}개"
            + (", 레포 밖 파일" if conditions["questionSet"].get("external") else "")
            + ")"
            if (conditions.get("questionSet") or {}).get("path")
            else ""
        ),
        f"- 시작: {conditions['startedAt']}, 상태: {conditions.get('status')}",
        f"- 문항 {len(conditions['questions'])}개, 반복 {conditions['repeats']}, "
        f"설정마다 워밍업 {conditions['warmup']}",
        f"- 비용 상한 ${conditions['maxUsd']}, 쓴 비용 ${conditions.get('spentUsd', 0):.4f}",
        "- 설정: "
        + "; ".join(
            f"{v['name']}(model={v.get('model')}, thinking={v.get('thinking')}, "
            f"effort={v.get('effort') or '-'}, maxTokens={v.get('maxTokens')}, "
            f"judgmentFacts={v.get('judgmentFacts')}, promptVersion={v.get('promptVersion')})"
            for v in conditions["variants"]
        ),
    ]
    if conditions["target"] == "spring":
        out.append("- spring 대상은 지연만 기록(토큰은 ai 대상에서 잰다).")
        out.append("- spring 대상은 H 문항의 history 를 보내지 못한다(대화 기록은 MySQL 에 있음).")
    if conditions.get("expectedToolsFrom"):
        source = conditions["expectedToolsFrom"]
        out.append(f"- 다시 채점: 기대 도구는 {source.get('file')} ({source.get('version')}) 기준")
    if conditions.get("error"):
        out.append(f"- 실패: {conditions['error']}")
    if conditions.get("unfinishedQuestions"):
        out.append(f"- 다 돌지 못한 문항: {', '.join(conditions['unfinishedQuestions'])}")
    if conditions.get("contextMatchesFixture") is False:
        out.append(FIXTURE_WARNING)
    for variant in conditions["variants"]:
        mine = measured_rows(rows, variant["name"])
        if not mine:
            continue
        done = [r for r in mine if r["status"] == "done"]
        first = [r["timings"]["firstDeltaMs"] for r in done if "firstDeltaMs" in r["timings"]]
        server_first = [
            r["serverTimings"]["firstDeltaMs"]
            for r in done
            if (r.get("serverTimings") or {}).get("firstDeltaMs") is not None
        ]
        answer_first = [
            r["metrics"]["firstAnswerDeltaMs"]
            for r in done
            if (r.get("metrics") or {}).get("firstAnswerDeltaMs") is not None
        ]
        measured = [r for r in mine if r.get("metrics")]
        pre_tool = [r for r in measured if (r["metrics"].get("preToolTextChars") or 0) > 0]
        total = [r["timings"]["doneMs"] for r in done if "doneMs" in r["timings"]]
        calls = [c for r in mine for c in (r.get("metrics") or {}).get("calls", [])]
        unsupported = [r for r in done if r["numberCheck"]["unsupported"] and not is_graded(r)]
        graded_done = [r for r in done if is_graded(r)]
        graded_numbers = [r for r in graded_done if r["numberCheck"]["unsupported"]]
        max_token_stops = sum(1 for c in calls if c.get("stopReason") == "max_tokens")
        out += [
            "",
            f"## {variant['name']}",
            "",
            f"- 호출 {len(mine)} (완료 {len(done)}, 오류 {len(mine) - len(done)})",
            f"- 첫 delta: {_stat(first)}",
            f"- 서버 firstDeltaMs(첫 text): {_stat(server_first)}",
            f"- 서버 firstAnswerDeltaMs(도구 결과 뒤 답변 호출의 첫 text): {_stat(answer_first)}",
            f"- 도구 전 text가 나온 회차: {len(pre_tool)}/{len(measured)}",
            f"- 전체: {_stat(total)}",
            f"- stop_reason=max_tokens: {max_token_stops}",
            "- 평균 thinking 블록(호출당): "
            + (
                f"{sum(c.get('thinkingBlocks') or 0 for c in calls) / len(mine):.2f}"
                if mine
                else "-"
            ),
            f"- 도구 선택 일치: {sum(1 for r in mine if r.get('toolMatch')) / len(mine):.0%}",
            f"- 근거 없는 숫자가 있는 답변(사람 채점 문항 제외): "
            f"{len(unsupported)}/{len(done) - len(graded_done)}"
            + (" — " + unsupported_detail(unsupported) if unsupported else ""),
        ]
        if any(r.get("rateUnit") for r in mine) and not any(r.get("remitCheck") for r in mine):
            # 기존 문항 세트에서 송금 도구를 쓴 답(v3 회귀). 송금 세트는 송금 재질의 절에 적는다.
            out.append(rate_unit_line(mine))
            unclear = [r for r in mine if (r.get("rateUnit") or {}).get("verdict") == "unclear"]
            if unclear:
                out.append(
                    "- 환율 단위 사람 확인: "
                    + ", ".join(f"{r['questionId']}#{r['repeat']}" for r in unclear)
                )
        if graded_done:
            out.append(
                f"- 사람 채점 문항에서 도구 결과에 없는 숫자(계산한 답일 수 있음, 판단형 표에서 "
                f"확인): {len(graded_numbers)}/{len(graded_done)}"
                + (" — " + unsupported_detail(graded_numbers) if graded_numbers else "")
            )
        calc = [r for r in done if r.get("referenceCheck")]
        if calc:
            verdicts = [r["referenceCheck"]["verdict"] for r in calc]
            out.append(
                f"- 계산 질문 {len(calc)}개: 참조값 포함 {verdicts.count('reference')} / "
                f"다른 숫자 {verdicts.count('wrong_number')} / 값 없음 {verdicts.count('no_value')}"
            )
            for r in calc:
                check = r["referenceCheck"]
                found = [t for texts in check["matched"].values() for t in texts]
                detail = {
                    "reference": f"참조값 포함 ({', '.join(found)})",
                    "wrong_number": f"다른 숫자 ({', '.join(check['unsupported'])})",
                    "no_value": "값 없음",
                }[check["verdict"]]
                out.append(f"  - {r['questionId']} #{r['repeat']}: {detail}")
        out += fx_summary_lines(mine)
        out += _lang_check.summary_lines(mine)
        out += _lang_check.improvement_lines(mine, answers=answers)
        out += source_split_table(mine)
        out += _product_check.summary_lines(mine)
        out += _product_check.tool_summary_lines(mine)
        out += [
            "",
            "| 문항 | 호출 | 입력 토큰 | 출력 토큰 | 비용(USD) |",
            "|---|---|---|---|---|",
        ]
        for qid in dict.fromkeys(r["questionId"] for r in mine):
            q_rows = [r for r in mine if r["questionId"] == qid]
            n = len(q_rows)
            usage = [r.get("usage") or {} for r in q_rows]
            out.append(
                f"| {qid} | {n} | {sum(u.get('inputTokens', 0) for u in usage) / n:.0f} | "
                f"{sum(u.get('outputTokens', 0) for u in usage) / n:.0f} | "
                f"{sum(r.get('costUsd', 0) for r in q_rows) / n:.5f} |"
            )
    out += remit_section(conditions, rows, answers=answers)
    if answers:
        out += judgment_section(conditions, rows)
    elif any(is_graded(r) and not r["warmup"] for r in rows):
        out += ["", "판단형 답변 전문은 요약 .md 파일에만 남긴다."]
    return "\n".join(out) + "\n"


def row_block_types(row: dict[str, Any]) -> list[str]:
    """이 행(송금 2턴이면 두 턴)의 done.blocks 종류."""
    follow = row.get("followUp") or {}
    blocks = [*(row.get("blocks") or []), *(follow.get("blocks") or [])]
    return [str(b.get("type")) for b in blocks]


def fx_summary_lines(mine: list[dict[str, Any]]) -> list[str]:
    """환율 문항(category fx)의 도구·카드·방향, 모든 행의 금지 표현과 카드 종류(#101)."""
    out: list[str] = []
    fx_rows = [r for r in mine if r.get("category") == "fx"]
    if fx_rows:
        n = len(fx_rows)
        called = sum(
            1 for r in fx_rows if any(t.get("toolName") == FX_RATE_SUMMARY for t in r["tools"])
        )
        legacy = sum(
            1 for r in fx_rows if any(t.get("toolName") == "get_fx_rate" for t in r["tools"])
        )
        cards = sum(1 for r in fx_rows if FX_SUMMARY_CARD in row_block_types(r))
        clean = sum(1 for r in fx_rows if not r["numberCheck"]["unsupported"])
        direction = [(r.get("fxCheck") or {}).get("direction") or {} for r in fx_rows]
        advice = [s for r in fx_rows for s in (r.get("fxCheck") or {}).get("advice") or []]
        out += [
            f"- 환율 문항 {n}회: fx_rate_summary 호출 {called}/{n}, FX_SUMMARY_CARD {cards}/{n}, "
            f"get_fx_rate 호출 {legacy}/{n}, 근거 없는 숫자 없음 {clean}/{n}",
            f"- 환율 방향: 불일치 {sum(d.get('mismatches', 0) for d in direction)}, "
            f"일치 {sum(d.get('matches', 0) for d in direction)}, "
            f"방향 낱말 없음 {sum(d.get('noDirection', 0) for d in direction)}, "
            f"예측·권유 자동 표시 {len(advice)}",
        ]
    hits = [(r, h) for r in mine for h in (r.get("forbidden") or {}).get("hits") or []]
    kinds = {k: sum(1 for _, h in hits if h["kind"] == k) for k in _fx_check.FORBIDDEN_KINDS}
    out.append("- 금지 표현(부정문 제외): " + ", ".join(f"{k} {v}" for k, v in kinds.items()))
    out += [f"  - {r['questionId']}#{r['repeat']} {h['kind']}: {h['text']}" for r, h in hits]
    types = [t for r in mine for t in set(row_block_types(r))]
    if types:
        out.append(
            "- 카드가 나온 회차: " + ", ".join(f"{t} {types.count(t)}" for t in sorted(set(types)))
        )
    return out


REMIT_OUTCOMES = {
    "correct": "정답",
    "missed_clarification": "놓친 재질의",
    "unnecessary_question": "불필요한 재질의",
    "wrong_guess": "잘못 추측",
    "asked_other": "다른 항목을 물음",
    "not_completed": "2턴에도 미완료",
    "out_of_scope_proceeded": "범위 밖인데 진행",
    "declined": "범위 밖 안내",
    "error": "오류",
    "unclear": "규칙으로 못 정함",
}


def _turn_cell(facts: dict[str, Any] | None) -> str:
    if facts is None:
        return "-"
    if facts["proceeded"]:
        quote = facts["quote"] or {}
        amount = quote.get("amount")
        detail = (
            f"({quote.get('recipient')} {quote.get('currency')} {amount:,})"
            if isinstance(amount, int)
            else ""
        )
        return ("카드" if facts["card"] else "견적만") + detail
    if facts["asked"]:
        return "질문(" + (", ".join(facts["askedAbout"]) or "?") + ")"
    return "답변"


def _expected_cell(row: dict[str, Any]) -> str:
    amount = row.get("expectedAmount")
    slots = [
        row.get("expectedRecipient") or "?",
        row.get("expectedCurrency") or "?",
        f"{amount:,}" if isinstance(amount, int) else "?",
    ]
    return f"{row['expectedAction']} ({', '.join(slots)})"


def rate_unit_line(rows: list[dict[str, Any]]) -> str:
    """환율 단위 집계: 환율을 말한 답변 수와 판정별 수(wrong은 문항#회차까지)."""
    judged = [r for r in rows if r.get("rateUnit")]
    counts = {
        v: [r for r in judged if r["rateUnit"]["verdict"] == v]
        for v in ("correct", "wrong", "no_unit", "unclear")
    }
    wrong = ", ".join(f"{r['questionId']}#{r['repeat']}" for r in counts["wrong"])
    return (
        f"- 환율 단위: 환율을 말한 답변 {len(judged)}/{len(rows)}, "
        f"correct {len(counts['correct'])}, wrong {len(counts['wrong'])}"
        + (f" ({wrong})" if wrong else "")
        + f", no_unit {len(counts['no_unit'])}, unclear {len(counts['unclear'])}"
    )


def remit_section(
    conditions: dict[str, Any], rows: list[dict[str, Any]], *, answers: bool
) -> list[str]:
    """송금 재질의 판정 집계와 문항별 표. answers=True(.md)일 때만 사람 확인 목록에 답변 전문."""
    out: list[str] = []
    for variant in conditions["variants"]:
        mine = [r for r in measured_rows(rows, variant["name"]) if r.get("remitCheck")]
        if not mine:
            continue
        checks = [r["remitCheck"] for r in mine]
        counts = {key: sum(1 for c in checks if c["outcome"] == key) for key in REMIT_OUTCOMES}
        turns = [c["turns"] for c in checks if c["outcome"] == "correct"]
        review = [r for r in mine if r["remitCheck"]["needsReview"]]
        out += [
            "",
            f"## 송금 재질의: {variant['name']}",
            "",
            "- 판정: "
            + ", ".join(f"{label} {counts[k]}" for k, label in REMIT_OUTCOMES.items() if counts[k]),
            f"- 정답의 완료 턴: 1턴 {turns.count(1)}, 2턴 {turns.count(2)}",
            f"- 근거 없는 숫자가 있는 문항: "
            f"{sum(1 for r in mine if r.get('numberCheck', {}).get('unsupported'))}/{len(mine)}",
            rate_unit_line(mine),
            f"- 사람 확인: {len(review)}/{len(mine)}",
            "",
            "| 문항 | 회차 | 기대 | 1턴 | 2턴 | 판정 | 완료 턴 | 추측 오류 | 근거 없는 숫자 | "
            "환율 단위 | 사람 확인 |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for r in mine:
            check = r["remitCheck"]
            out.append(
                f"| {r['questionId']} | {r['repeat']} | {_expected_cell(r)} | "
                f"{_turn_cell(check['first'])} | {_turn_cell(check['second'])} | "
                f"{REMIT_OUTCOMES[check['outcome']]} | {check['turns'] or '-'} | "
                f"{', '.join(check['guessErrors']) or '-'} | "
                f"{_cell(', '.join(r['numberCheck']['unsupported'])) or '-'} | "
                f"{(r.get('rateUnit') or {}).get('verdict') or '-'} | "
                f"{_cell(check['reason']) if check['needsReview'] else '-'} |"
            )
        if review and answers:
            out += [
                "",
                f"### 사람 확인 목록: {variant['name']}",
                "",
                "| 문항 | 회차 | 이유 | 질문 | 1턴 답변 | 2턴(답) | 2턴 답변 |",
                "|---|---|---|---|---|---|---|",
            ]
            for r in review:
                follow = r.get("followUp") or {}
                out.append(
                    f"| {r['questionId']} | {r['repeat']} | {_cell(r['remitCheck']['reason'])} | "
                    f"{_cell(r['contextTexts'][0])} | {_cell(r.get('answer') or '')} | "
                    f"{_cell(r.get('followUpReply') or '-') if follow else '-'} | "
                    f"{_cell(follow.get('answer') or '-')} |"
                )
        elif review:
            out += ["", "사람 확인 목록(답변 전문)은 요약 .md 파일에만 남긴다."]
    return out


HOLDOUT_SOURCE = "holdout_v1"
FIXTURE_WARNING = (
    "- 주의: 이 run의 도구 데이터가 데모 fixture와 다르다. 기대 결론·참조값은 fixture 기준이라 "
    "채점 기준이 맞지 않을 수 있다."
)


def source_split_table(mine: list[dict[str, Any]]) -> list[str]:
    """Existing questions vs hold-out (source holdout_v1), from saved fields only (rescore-safe).

    Counts only; the verdicts are the ones score() already made.
    """
    out = [
        "",
        "| 구분 | 판단형 채점 목록 | 계산 참조값 (포함 / 다른 숫자 / 값 없음) | "
        "근거 없는 숫자, 채점 문항 제외 (답변 / 숫자 수) | "
        "채점 문항의 표에 없는 숫자 (답변 / 숫자 수) |",
        "|---|---|---|---|---|",
    ]
    for label, group in (
        ("기존 문항", [r for r in mine if r.get("source") != HOLDOUT_SOURCE]),
        ("hold-out", [r for r in mine if r.get("source") == HOLDOUT_SOURCE]),
    ):
        done = [r for r in group if r["status"] == "done"]
        graded = sum(1 for r in group if is_graded(r))
        verdicts = [r["referenceCheck"]["verdict"] for r in done if r.get("referenceCheck")]
        cells = []
        for part in ([r for r in done if not is_graded(r)], [r for r in done if is_graded(r)]):
            unsupported = [r["numberCheck"]["unsupported"] for r in part]
            cells.append(
                f"{sum(1 for u in unsupported if u)}/{len(part)} / "
                f"{sum(len(u) for u in unsupported)}"
            )
        out.append(
            f"| {label} | {graded} | {len(verdicts)}개: {verdicts.count('reference')} / "
            f"{verdicts.count('wrong_number')} / {verdicts.count('no_value')} | "
            f"{cells[0]} | {cells[1]} |"
        )
    return out


def _cell(text: str) -> str:
    """One markdown table cell: line breaks become spaces, pipes are escaped."""
    return " ".join(text.split()).replace("|", "\\|")


def judgment_section(conditions: dict[str, Any], rows: list[dict[str, Any]]) -> list[str]:
    """Judgment answers next to the expected conclusion, for a person to grade (no auto score)."""
    blocks: list[list[str]] = []
    for variant in conditions["variants"]:
        mine = [r for r in measured_rows(rows, variant["name"]) if is_graded(r)]
        if not mine:
            continue
        block = [
            "",
            f"### {variant['name']}",
            "",
            "| 문항 | 회차 | 기대 결론 | 답변 |",
            "|---|---|---|---|",
        ]
        for r in mine:
            answer = r.get("answer") or ""
            if r["status"] != "done":
                answer = f"(오류 {r.get('errorCode') or r['status']}) {answer}"
            block.append(
                f"| {r['questionId']} | {r['repeat']} | {_cell(r['expectedConclusion'])} | "
                f"{_cell(answer)} |"
            )
        blocks.append(block)
    if not blocks:
        return []
    out = ["", "## 판단형 문항(사람 채점)"]
    if conditions.get("contextMatchesFixture") is False:
        out += ["", FIXTURE_WARNING]
    for block in blocks:
        out += block
    return out


def write_jsonl(path: Path, conditions: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "conditions", **conditions}, ensure_ascii=False) + "\n")
        for row in rows:
            fh.write(json.dumps({"type": "call", **row}, ensure_ascii=False) + "\n")
    tmp.replace(path)
    path.with_suffix(".md").write_text(render_summary(conditions, rows), encoding="utf-8")


def read_jsonl(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    conditions: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        kind = item.pop("type", None)
        if kind == "conditions":
            conditions = item
        elif kind == "call":
            rows.append(item)
    return conditions, rows


def progress_line(done: int, total: int, row: dict[str, Any], spent: float) -> str:
    t = row["timings"]
    return (
        f"[{done}/{total}] {row['variant']} {row['questionId']} #{row['repeat']}"
        f"{' (warmup)' if row['warmup'] else ''} {row['status']}"
        f"{':' + str(row.get('errorCode')) if row.get('errorCode') else ''} "
        f"first={t.get('firstDeltaMs', '-')}ms done={t.get('doneMs', '-')}ms "
        f"in={(row.get('usage') or {}).get('inputTokens', '-')} "
        f"out={(row.get('usage') or {}).get('outputTokens', '-')} "
        f"${row.get('costUsd', 0):.4f} total=${spent:.4f}"
    )


def main(
    argv: list[str] | None = None,
    *,
    client: httpx.Client | None = None,
    settings: Settings | None = None,
    confirm: Callable[[str], str] = input,
) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--target", choices=["ai", "spring"], default="ai")
    parser.add_argument(
        "--llm-mode",
        choices=["fake", "anthropic"],
        default="fake",
        help="what the server runs; anthropic asks before paid calls (not read from env)",
    )
    parser.add_argument("--base-url", default=None, help="ai: http://localhost:8000, spring: :8080")
    parser.add_argument("--jwt", default=None, help="spring target: access token from /auth/entry")
    parser.add_argument("--user-id", type=int, default=1, help="ai target userId")
    parser.add_argument("--variants", type=Path, default=None)
    parser.add_argument("--questions", default=None, help="comma-separated ids, e.g. F01,F03")
    parser.add_argument("--subset", choices=["latency"], default=None)
    parser.add_argument(
        "--question-set",
        choices=sorted(QUESTION_SETS),
        default="eval",
        help="eval: eval_questions_v1.json, remit: remit_questions_v1.json (prompt v3), "
        "product: product_questions_v1.json (product tool, #65), "
        "policy: specs/rag/finance_policy_questions_v1.json (finance policy RAG, #88), "
        "v44: v44_questions_v1.json (FX summary, E-7-4 and remittance cards, #101), "
        "lang: answer_language_questions_v1.json (answer language, #106)",
    )
    parser.add_argument(
        "--language",
        choices=["en", "ko", "vi", "zh"],
        default="en",
        help="ai target: the user's language sent to the agent (#106). spring target: the "
        "user's preferred_language in MySQL is used, so only en is accepted here",
    )
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--warmup", type=int, default=0, help="calls per variant, not summarized")
    parser.add_argument("--output", type=Path, default=None, help="JSONL path (required)")
    parser.add_argument(
        "--remit-questions",
        type=Path,
        default=None,
        help="remit question file (same schema as remit_questions_v1.json, may be outside the "
        "repo); implies --question-set remit",
    )
    parser.add_argument(
        "--product-questions",
        type=Path,
        default=None,
        help="product question file (same schema as product_questions_v1.json, may be outside "
        "the repo, e.g. the hold-out); implies --question-set product",
    )
    parser.add_argument(
        "--policy-questions",
        type=Path,
        default=None,
        help="finance policy question file (same schema as finance_policy_questions_v1.json, may "
        "be outside the repo, e.g. the hold-out; the file is read, never printed); implies "
        "--question-set policy",
    )
    parser.add_argument("--max-usd", type=float, default=1.0)
    parser.add_argument("--rescore", type=Path, default=None, help="re-score a saved JSONL")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args(argv)

    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.output is None:
        print("REFUSED: --output is required (results are saved after every call)")
        return EXIT_USAGE

    if args.rescore is not None:
        saved_conditions, saved_rows = read_jsonl(args.rescore)
        refreshed = refresh_expected_tools(saved_conditions, saved_rows)
        for saved in saved_rows:
            score(saved)
        saved_conditions["rescoredFrom"] = str(args.rescore)
        if refreshed:
            saved_conditions["expectedToolsFrom"] = refreshed
        write_jsonl(args.output, saved_conditions, saved_rows)
        print(f"rescored {len(saved_rows)} calls -> {args.output} (+ .md)")
        return EXIT_OK

    settings = settings or get_settings()
    if args.target == "spring" and not args.jwt:
        parser.error("--target spring needs --jwt")
    if args.target == "spring" and args.variants:
        parser.error(
            "--variants works with --target ai only (Spring does not forward X-Chat-Config)"
        )
    if args.repeats < 1 or args.warmup < 0:
        parser.error("--repeats must be >= 1 and --warmup >= 0")
    if args.target == "spring" and args.language != "en":
        parser.error(
            "--language works with --target ai only (Spring sends users.preferred_language)"
        )
    if args.target == "ai" and not settings.internal_token_configured():
        print("BLOCKED: AI_INTERNAL_API_TOKEN is not configured")
        return EXIT_BLOCKED
    product_external = args.product_questions is not None
    policy_external = args.policy_questions is not None
    given = [args.remit_questions, args.product_questions, args.policy_questions]
    if sum(path is not None for path in given) > 1:
        parser.error(
            "--remit-questions, --product-questions and --policy-questions cannot be combined"
        )
    external = args.remit_questions is not None or product_external or policy_external
    remit = args.question_set == "remit" or args.remit_questions is not None
    if remit and args.subset:
        parser.error("--subset works with --question-set eval only")
    question_file = (
        args.remit_questions
        if args.remit_questions is not None
        else args.product_questions
        if product_external
        else args.policy_questions
        if policy_external
        else QUESTION_SETS[args.question_set]
    )
    try:
        spec = load_questions(question_file)
    except (OSError, ValueError) as exc:
        parser.error(f"cannot read {question_file}: {type(exc).__name__}")
    if remit:
        errors = remit_spec_errors(spec)
        if errors:
            parser.error(f"{question_file}: " + "; ".join(errors[:5]))
    questions = select_questions(spec, args.questions, args.subset, parser)
    if remit:
        questions = [remit_question(q) for q in questions]
    questions = [with_profile_header(spec, q) for q in questions]
    try:
        variants = load_variants(args.variants, settings)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    fake = args.llm_mode == "fake"
    base_url = args.base_url or (
        "http://localhost:8000" if args.target == "ai" else "http://localhost:8080"
    )

    total_calls = len(variants) * (args.warmup + len(questions) * args.repeats)
    estimates = {v["name"]: estimate_call_usd(v["model"], v["maxTokens"], fake) for v in variants}

    def turns_of(question: dict[str, Any]) -> int:
        # 애매한 송금 문항은 2턴까지 간다(예산은 2턴으로 잡는다).
        return 2 if question.get("followUpReply") else 1

    planned_turns = sum(turns_of(q) for q in questions) * args.repeats
    planned_max = sum(estimates[v["name"]] * (args.warmup + planned_turns) for v in variants)
    print(f"llm-mode={args.llm_mode} target={args.target} base={base_url} language={args.language}")
    for v in variants:
        print(
            f"  {v['name']}: model={v['model']} thinking={v.get('thinking')} "
            f"effort={v.get('effort') or '-'} maxTokens={v['maxTokens']} "
            f"judgmentFacts={v.get('judgmentFacts')} promptVersion={v.get('promptVersion')} "
            f"(<= ${estimates[v['name']]:.4f}/call)"
        )
    print(
        f"총 {total_calls}회 호출(설정 {len(variants)} × (워밍업 {args.warmup} + 문항 "
        f"{len(questions)} × 반복 {args.repeats}))"
        + (f", 2턴 포함 최대 {len(variants) * (args.warmup + planned_turns)}턴" if remit else "")
        + f", 예상 최대 ${planned_max:.4f} "
        f"(호출당 LLM {EST_LLM_CALLS}회·출력 maxTokens 가정), 상한 ${args.max_usd}",
        flush=True,
    )
    if planned_max > args.max_usd:
        tail = ", ".join(q["id"] for q in questions[-3:])
        print(
            "WARN: 예상 최대가 상한보다 큽니다. 문항 순서대로 돌기 때문에 "
            f"뒤쪽 문항({tail} 등)부터 잘릴 수 있습니다. "
            "--max-usd를 올리거나 --questions로 나눠 실행하세요.",
            flush=True,
        )
    if not fake and not args.yes and confirm("계속할까요? [y/N] ").strip().lower() != "y":
        print("cancelled")
        return EXIT_OK

    context = tool_context(settings, args.user_id, args.llm_mode)
    # 송금 도구가 붙는 버전(v3)을 재면 기존 문항 세트에서도 송금 도구 데이터를 남긴다(숫자 근거).
    uses_remittance = remit or any(
        v.get("promptVersion") in REMITTANCE_PROMPT_VERSIONS for v in variants
    )
    remit_ctx = remit_context(settings, args.user_id, args.llm_mode) if uses_remittance else None
    matches_fixture = context == fixture_context()
    if not matches_fixture:
        print("WARN: " + FIXTURE_WARNING[len("- 주의: ") :], flush=True)
    conditions: dict[str, Any] = {
        "target": args.target,
        "mode": args.llm_mode,
        "baseUrl": base_url,
        "userId": args.user_id,
        # 사용자 언어(#106). ai 대상은 요청 language, spring 대상은 MySQL 값(여기서는 en으로 둠).
        "language": args.language,
        "variants": [{k: v for k, v in variant.items() if k != "override"} for variant in variants],
        "questionSet": {
            "file": question_file.name,
            "version": spec.get("version"),
            "path": str(question_file.resolve()),
            "count": len(spec["questions"]),
            "external": external,
        },
        "questions": [q["id"] for q in questions],
        "repeats": args.repeats,
        "warmup": args.warmup,
        "maxUsd": args.max_usd,
        "startedAt": datetime.now(UTC).isoformat(),
        "status": "running",
        "spentUsd": 0.0,
        "contextMatchesFixture": matches_fixture,
    }
    rows: list[dict[str, Any]] = []
    # Highest paid cost seen per variant; until one is seen the estimate is used (a call that
    # failed at no cost says nothing about the next one).
    observed: dict[str, float] = {}
    owns_client = client is None
    http = client or httpx.Client()
    token = settings.ai_internal_api_token.get_secret_value()

    schedule: list[tuple[dict[str, Any], dict[str, Any], int, bool]] = []
    for variant in variants:
        for i in range(args.warmup):
            schedule.append((variant, questions[i % len(questions)], i + 1, True))
    for question in questions:
        for repeat in range(1, args.repeats + 1):
            for variant in variants:  # round-robin inside the same question and repeat
                schedule.append((variant, question, repeat, False))

    def save(status: str) -> None:
        conditions["status"] = status
        conditions["spentUsd"] = round(sum(r.get("costUsd", 0) for r in rows), 6)
        write_jsonl(args.output, conditions, rows)

    def unfinished() -> list[str]:
        per_question = len(variants) * args.repeats
        counts = {q["id"]: 0 for q in questions}
        for r in rows:
            if not r["warmup"]:
                counts[r["questionId"]] += 1
        return [qid for qid, n in counts.items() if n < per_question]

    try:
        for variant, question, repeat, warmup in schedule:
            spent = sum(r.get("costUsd", 0) for r in rows)
            # 관측값은 한 행(2턴이면 두 턴 합) 비용의 최댓값이라 턴 수를 곱하면 넉넉하게 잡힌다.
            per_turn = observed.get(variant["name"], estimates[variant["name"]])
            next_cost = per_turn * turns_of(question)
            if spent + next_cost > args.max_usd:
                print(f"budget stop: spent ${spent:.4f} + next ~${next_cost:.4f} > ${args.max_usd}")
                conditions["unfinishedQuestions"] = unfinished()
                print(f"다 돌지 못한 문항: {', '.join(conditions['unfinishedQuestions'])}")
                save("budget_stopped")
                print(render_summary(conditions, rows, answers=False))
                return EXIT_OK
            if args.target == "ai":
                result = call_ai(
                    http, base_url, token, question, variant, args.user_id, args.language
                )
            else:
                result = call_spring(http, base_url, args.jwt, question)
            done = result.get("done") or {}
            metrics = result.get("metrics")
            answered_by = answering_model(done, metrics)
            model = answered_by or (metrics or {}).get("config", {}).get("model")
            usage = run_usage(done, metrics)
            prompt_version = row_prompt_version(variant, done, metrics)
            answer = "".join(result["deltas"])
            follow_up = None
            if remit and question.get("followUpReply") and result.get("status") == "done":
                first_quoted = any(
                    t.get("toolName") == QUOTE_REMITTANCE for t in result["tools"]
                ) or any(b.get("type") == "REMITTANCE_CONFIRM" for b in done.get("blocks") or [])
                if not first_quoted:
                    follow_up = run_follow_up(
                        http,
                        base_url,
                        token,
                        question,
                        answer,
                        variant,
                        args.user_id,
                        args.language,
                    )
            row: dict[str, Any] = {
                "variant": variant["name"],
                "questionId": question["id"],
                "category": question["category"],
                "repeat": repeat,
                "warmup": warmup,
                "language": args.language,
                # LE03(#106): 개선 방안 언급 보조 지표를 붙일 문항.
                "wantsImprovementCheck": bool(question.get("improvementCheck")),
                "at": datetime.now(UTC).isoformat(),
                "status": result.get("status"),
                "httpStatus": result.get("httpStatus"),
                "errorCode": result.get("errorCode"),
                "events": result["events"],
                "timings": result["timings"],
                "serverTimings": done.get("timings") or {"latencyMs": done.get("latencyMs")},
                "usage": usage,
                "metrics": metrics,
                "model": model,
                "costUsd": round(
                    cost_usd(usage, model) + (follow_up["costUsd"] if follow_up else 0), 6
                ),
                "tools": result["tools"],
                "answer": answer,
                "blocks": done.get("blocks"),
                "deltaTimesMs": result["deltaMs"],
                # 기대 도구는 프롬프트 버전마다 다를 수 있다(byPromptVersion).
                "promptVersion": prompt_version,
                "expectedTools": expected_tools(question, prompt_version),
                "referenceValues": question.get("referenceValues"),
                # 상품 문항(#65)만 있다. productCheck를 붙이는 기준.
                "expectedOutcome": question_outcome(spec, question),
                **product_fields(spec, question),
                "productTool": product_tool_result(metrics),
                "policyTools": policy_tool_results(metrics),
                "fxTools": fx_tool_results(metrics),
                "remitTools": remit_tool_results(metrics),
                # Listed for a person to grade: judgment questions and humanGraded ones only
                # (other calculation questions keep their conclusion in the spec, not here).
                "expectedConclusion": (
                    question.get("expectedConclusion")
                    if question["category"] == "judgment" or question.get("humanGraded")
                    else None
                ),
                "source": question.get("source"),
                "judgmentFacts": bool(variant.get("judgmentFacts")),
                "toolResult": context,
                "contextTexts": [
                    question["text"],
                    *(h["content"] for h in question.get("history", [])),
                    *([question["followUpReply"]] if question.get("followUpReply") else []),
                ],
            }
            if remit:
                row.update(
                    {
                        key: question[key]
                        for key in ("expectedAction", *REMIT_SLOTS, "followUpReply")
                    }
                )
                row["followUp"] = follow_up
                # 2턴 뒤 기대값은 고정 세트에만 있다(외부 질문 파일은 id가 겹쳐도 쓰지 않는다).
                final = None if external else FOLLOW_UP_EXPECTED.get(question["id"])
                row["followUpExpected"] = list(final) if final else None
            if remit_ctx is not None:
                row["remitContext"] = remit_ctx
            score(row)
            rows.append(row)
            if row["costUsd"] > 0:
                observed[variant["name"]] = max(observed.get(variant["name"], 0.0), row["costUsd"])
            save("running")
            print(progress_line(len(rows), total_calls, row, conditions["spentUsd"]), flush=True)
            used_tokens = any((usage or {}).get(k) for k in USAGE_KEYS)
            if fake and (answered_by not in (None, FAKE_MODEL_ID) or used_tokens):
                print(
                    "STOP: 서버가 실제 모드입니다(--llm-mode fake인데 응답에 실제 모델이나 "
                    "토큰 사용량이 있음). 서버를 fake로 띄우거나 --llm-mode anthropic으로 "
                    "다시 실행하세요."
                )
                save("mode_mismatch")
                return EXIT_BLOCKED
            confirmed = variant.get("promptVersionFromServer")
            adopted = adopt_server_prompt_version(variant, metrics) if args.target == "ai" else None
            if not confirmed and variant.get("promptVersionFromServer"):
                # 조건 기록(첫 줄)에도 서버가 쓴 버전을 남긴다.
                conditions["variants"] = [
                    {k: v for k, v in item.items() if k != "override"} for item in variants
                ]
            if adopted is not None:
                print(
                    f"NOTE: {variant['name']}의 promptVersion을 서버 보고값 "
                    f"{variant['promptVersion']}로 맞춤(이 프로세스 설정은 {adopted})",
                    flush=True,
                )
            mismatch = (
                config_mismatch(variant, answered_by, metrics, fake) if args.target == "ai" else []
            )
            if mismatch:
                print(
                    f"STOP: 서버가 {variant['name']} 설정대로 돌지 않았습니다"
                    f"({'; '.join(mismatch)}). "
                    "--variants를 쓰면 서버를 CHAT_ALLOW_CONFIG_OVERRIDE=true로 띄우세요."
                )
                save("config_mismatch")
                return EXIT_BLOCKED
    except KeyboardInterrupt:
        save("interrupted")
        print(f"interrupted after {len(rows)}/{total_calls} calls")
        return EXIT_INTERRUPTED
    except httpx.HTTPError as exc:
        # The exception text can hold the URL only; no token or answer is in it.
        conditions["error"] = f"{type(exc).__name__}: {exc}"
        conditions["unfinishedQuestions"] = unfinished()
        save("failed")
        print(f"FAILED after {len(rows)}/{total_calls} calls: {conditions['error']}")
        print(render_summary(conditions, rows, answers=False))
        return EXIT_FAILED
    finally:
        if owns_client:
            http.close()

    save("completed")
    print(render_summary(conditions, rows, answers=False))
    print(json.dumps({"output": str(args.output), "summary": str(args.output.with_suffix(".md"))}))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

"""단계 제목·설명 문장. 기본은 템플릿, 설정으로 켜면 LLM이 다듬는다.

LLM에는 단계 데이터만 넘긴다. 응답 문장에 단계 데이터에 없는 숫자나 금지 표현이 있으면
LLM 문장을 버리고 템플릿 문장을 쓴다(숫자는 모두 엔진이 만든 것만 보여준다).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from app.visa.e74.improvements import Improvement, ImprovementKind
from app.visa.e74.rules import (
    Contract,
    CurrentEmployment,
    DriverLicense,
    EmployerRecommendation,
    EvidencePresent,
    KoreanLevel,
    MinAverageIncome,
    MinKorean,
    Qualification,
    Rule,
    RuleCategory,
)
from app.visa.e74.schemas import EvidenceType

logger = logging.getLogger(__name__)

PROMPT_VERSION = "visa-roadmap-text-v3"
MAX_OUTPUT_TOKENS = 2048
MAX_TITLE_CHARS = 40
MAX_DESCRIPTION_CHARS = 300

# 모의 사전진단이다. 승인 가능성·순위처럼 들리는 표현은 쓰지 않는다.
FORBIDDEN_PHRASES = ("합격", "불합격", "상위", "가능성", "확률", "보장", "확실")
# 금지 표현의 규칙 코드. 대체 로그에는 문장 대신 이 코드만 남긴다.
FORBIDDEN_PHRASE_CODES = {
    "합격": "PASS",
    "불합격": "FAIL",
    "상위": "RANK",
    "가능성": "LIKELIHOOD",
    "확률": "PROBABILITY",
    "보장": "GUARANTEE",
    "확실": "CERTAINTY",
}

# 안내·면책 문장 패턴(규칙 코드 → 구절). 화면이 고정 문구로 표시하므로 LLM 문장에서 지운다.
# "승인 신청"처럼 정상 설명을 지우지 않게 단어가 아니라 구절로 잡는다.
DISCLAIMER_PATTERNS: dict[str, tuple[str, ...]] = {
    "DISCLAIMER_MOCK": ("모의 사전진단", "모의 진단", "사전진단 결과"),
    "DISCLAIMER_APPROVAL": ("승인 여부", "승인될", "승인되지", "승인 가능", "승인을 보장"),
    "DISCLAIMER_VARIES": ("달라질 수 있",),
    "DISCLAIMER_GUARANTEE": ("보장하지", "보장되지", "보장할 수 없"),
}

_NUMBER = re.compile(r"\d[\d,]*")
_SENTENCE_END = re.compile(r"(?<=[.!?。])\s+")


@dataclass(frozen=True)
class StepText:
    title: str
    description: str


# --- 템플릿 ------------------------------------------------------------------------------


def _title(imp: Improvement, rule: Rule) -> str:
    c = rule.condition
    kind = imp.kind
    if kind is ImprovementKind.SUBMIT_EVIDENCE:
        if rule.category is RuleCategory.BASE_INCOME:
            return "소득금액증명원 제출"
        if rule.category is RuleCategory.BASE_KOREAN:
            return "한국어 성적 증빙 제출"
        if (
            isinstance(c, EvidencePresent)
            and c.evidence_type is EvidenceType.RECOMMENDATION_EMPLOYER
        ):
            return "고용기업 추천서 제출"
        return "증빙 서류 제출"
    if kind is ImprovementKind.RAISE_LEVEL:
        if isinstance(c, KoreanLevel):
            return f"TOPIK {c.topik_level_min}급 취득"
        return "최근 2년 평균소득 올리기"
    if kind is ImprovementKind.OBTAIN_BONUS:
        if isinstance(c, EvidencePresent):
            if c.evidence_type is EvidenceType.RECOMMENDATION_EMPLOYER:
                return "고용기업 추천서 받기"
            return "중앙부처·광역지자체 추천 받기"
        if isinstance(c, Qualification):
            return "국내 자격증·학위 취득"
        if isinstance(c, DriverLicense):
            return "국내 운전면허 취득"
        return "가점 받기"
    if isinstance(c, MinAverageIncome):
        return "최근 2년 평균소득 기준 맞추기"
    if isinstance(c, MinKorean):
        return "한국어 최소 기준 맞추기"
    if isinstance(c, Contract):
        return "E-7-4 고용계약 조건 맞추기"
    if isinstance(c, CurrentEmployment):
        return "현재 근무처에서 계속 일하기"
    if isinstance(c, EmployerRecommendation):
        return "고용기업 추천 조건 갖추기"
    return "필수 요건 갖추기"


def template_step_text(imp: Improvement, rule: Rule, *, required: bool) -> StepText:
    description = imp.description
    if required and imp.kind is not ImprovementKind.FIX_REQUIREMENT:
        description += ". 신청하려면 꼭 필요함"
    return StepText(title=_title(imp, rule), description=description)


def template_apply_text(applicable: date | None, expires: date) -> StepText:
    title = "E-7-4 전환 신청"
    if applicable is None:
        return StepText(
            title, "체류기간 정보를 확인하면 신청 가능일을 계산할 수 있음(아래 질문에 답해 주세요)"
        )
    if applicable > expires:
        return StepText(
            title,
            f"신청 가능일({applicable.isoformat()})보다 체류 만료일({expires.isoformat()})이 "
            "먼저 옴. 체류기간 연장 여부를 먼저 확인해야 함",
        )
    return StepText(
        title,
        f"{applicable.isoformat()}부터 체류 만료일({expires.isoformat()}) 전까지 "
        "출입국·외국인관서에 신청",
    )


# --- LLM ---------------------------------------------------------------------------------


class ClaudeClient(Protocol):
    """anthropic.Anthropic과 같은 모양의 클라이언트(messages.create). 테스트에서 가짜로 바꾼다."""

    @property
    def messages(self) -> Any: ...


SYSTEM_PROMPT = "\n".join(
    [
        f"[{PROMPT_VERSION}]",
        "외국인 근로자의 숙련기능인력(E-7-4) 비자 전환 준비 단계를 설명하는 문장을 써요.",
        "각 단계마다 title(20자 안팎)과 description(1~2문장, 해요체)을 한국어로 써요.",
        "규칙:",
        "- 숫자·날짜·금액·점수는 입력 데이터에 있는 것만 그대로 써요. "
        "계산하거나 새로 만들지 않아요.",
        "- 입력에 없는 요건·서류·기관을 더하지 않아요.",
        "- 이 결과는 모의 사전진단이에요. "
        "승인 가능성, 합격, 순위(상위 몇 %), 기준 점수를 말하지 않아요.",
        "- 이모지와 마크다운을 쓰지 않아요.",
        "- order는 입력과 같게, 모든 단계를 빠짐없이 돌려줘요.",
    ]
)

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "order": {"type": "integer"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["order", "title", "description"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["steps"],
    "additionalProperties": False,
}


def numbers_in(text: str) -> set[int]:
    """글에 나온 숫자(쉼표 제거). 날짜 2026-12-05는 2026, 12, 5로 본다."""
    return {int(m.replace(",", "")) for m in _NUMBER.findall(text) if m.replace(",", "")}


class LlmTextError(Exception):
    """LLM 문장을 쓸 수 없음. reason은 응답 text.fallbackReason에 그대로 들어간다.

    rule_codes는 걸린 금지 표현의 규칙 코드(FORBIDDEN_PHRASE일 때만). 문장 원문은 담지 않는다.
    """

    def __init__(self, reason: str, rule_codes: tuple[str, ...] = ()) -> None:
        super().__init__(reason)
        self.reason = reason
        self.rule_codes = rule_codes


def strip_disclaimers(description: str) -> tuple[str, set[str]]:
    """면책 패턴이 있는 문장을 지운 설명과 지운 패턴의 규칙 코드."""
    kept: list[str] = []
    removed: set[str] = set()
    for sentence in _SENTENCE_END.split(description.strip()):
        codes = {
            code
            for code, phrases in DISCLAIMER_PATTERNS.items()
            if any(p in sentence for p in phrases)
        }
        if codes:
            removed |= codes
        else:
            kept.append(sentence)
    return " ".join(kept).strip(), removed


def _without_disclaimers(texts: dict[int, StepText]) -> dict[int, StepText]:
    """면책 문장을 지운다. 지우고 나서 설명이 비면 LlmTextError(DISCLAIMER_ONLY)."""
    cleaned: dict[int, StepText] = {}
    removed: set[str] = set()
    for order, text in texts.items():
        description, codes = strip_disclaimers(text.description)
        removed |= codes
        if not description:
            raise LlmTextError("DISCLAIMER_ONLY", tuple(sorted(removed)))
        cleaned[order] = StepText(text.title, description)
    if removed:
        # 규칙 코드만 남긴다(문장 원문은 남기지 않음).
        logger.info("roadmap text disclaimers removed: rules=%s", ",".join(sorted(removed)))
    return cleaned


def check_llm_texts(
    steps: Sequence[dict[str, Any]], texts: dict[int, StepText]
) -> dict[int, StepText]:
    """면책 문장을 지운 뒤, 단계 데이터에 없는 숫자·금지 표현·빠진 단계가 있으면 LlmTextError."""
    allowed = numbers_in(json.dumps(steps, ensure_ascii=False))
    if set(texts) != {s["order"] for s in steps}:
        raise LlmTextError("INVALID_OUTPUT")
    texts = _without_disclaimers(texts)
    for text in texts.values():
        if (
            not text.title.strip()
            or not text.description.strip()
            or len(text.title) > MAX_TITLE_CHARS
            or len(text.description) > MAX_DESCRIPTION_CHARS
        ):
            raise LlmTextError("INVALID_OUTPUT")
    bodies = [f"{t.title}\n{t.description}" for t in texts.values()]
    hits = sorted({FORBIDDEN_PHRASE_CODES[p] for p in FORBIDDEN_PHRASES for b in bodies if p in b})
    if hits:
        raise LlmTextError("FORBIDDEN_PHRASE", tuple(hits))
    for body in bodies:
        if not numbers_in(body) <= allowed:
            raise LlmTextError("UNVERIFIED_NUMBER")
    return texts


class LlmStepWriter:
    """Claude로 단계 문장을 쓴다. 실패하면 LlmTextError를 올리고 호출 쪽이 템플릿을 쓴다."""

    prompt_version = PROMPT_VERSION

    def __init__(self, client: ClaudeClient, model_id: str) -> None:
        self._client = client
        self.model_id = model_id

    def write(self, steps: Sequence[dict[str, Any]]) -> dict[int, StepText]:
        try:
            response = self._client.messages.create(
                model=self.model_id,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[
                    {"role": "user", "content": json.dumps({"steps": steps}, ensure_ascii=False)}
                ],
                output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
                # 문장 다듬기라 추론이 필요 없다. 끄지 않으면 모델이 호출마다 thinking 여부를 정해
                # 응답 시간이 2배 이상 흔들린다(측정: 출력 토큰 약 450 ↔ 1,200).
                thinking={"type": "disabled"},
            )
        except Exception as exc:  # 네트워크·인증·한도 오류 모두 템플릿으로 되돌린다
            logger.warning("roadmap text LLM call failed: %s", type(exc).__name__)
            raise LlmTextError("LLM_ERROR") from exc
        if getattr(response, "stop_reason", None) != "end_turn":
            raise LlmTextError("INVALID_OUTPUT")
        raw = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
        try:
            rows = json.loads(raw)["steps"]
            texts = {
                int(r["order"]): StepText(str(r["title"]).strip(), str(r["description"]).strip())
                for r in rows
            }
        except (ValueError, KeyError, TypeError) as exc:
            raise LlmTextError("INVALID_OUTPUT") from exc
        return check_llm_texts(steps, texts)

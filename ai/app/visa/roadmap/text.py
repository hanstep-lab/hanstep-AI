"""단계 제목·설명 문장. 기본은 템플릿, 설정으로 켜면 LLM이 다듬는다.

LLM에는 단계 데이터만 넘긴다. 응답 문장에 단계 데이터에 없는 숫자나 금지 표현이 있으면
LLM 문장을 버리고 템플릿 문장을 쓴다(숫자는 모두 엔진이 만든 것만 보여준다).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Sequence
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
from app.visa.e74.texts import KOREAN, is_korean, rule_text

logger = logging.getLogger(__name__)

PROMPT_VERSION = "visa-roadmap-text-v4"
MAX_OUTPUT_TOKENS = 2048
MAX_TITLE_CHARS = 40
MAX_DESCRIPTION_CHARS = 300
# 문장 생성 전체(thinking 거부 후 재시도 포함)에 쓰는 시간. 넘기면 템플릿으로 되돌린다.
DEFAULT_TIMEOUT_SECONDS = 15.0
# 남은 시간이 이보다 적으면 thinking 거부 후 재시도를 하지 않는다(응답이 올 수 없는 시간).
MIN_RETRY_SECONDS = 3.0

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
# 문구는 (한국어, 영어) 표로 둔다. ko면 한국어(예전과 같은 글자), 그 밖(en·vi·zh)이면 영어.
# 개선 방안 설명·서류 이름·규칙 설명의 영어는 엔진 값(descriptionEn·documentsEn, texts.RULES)을
# 쓰고, 영어가 없으면(#108 전 저장 결과 등) 한국어를 그대로 둔다.

TITLES: dict[str, tuple[str, str]] = {
    "SUBMIT_INCOME": ("소득금액증명원 제출", "Submit your income certificate"),
    "SUBMIT_KOREAN": ("한국어 성적 증빙 제출", "Submit your Korean test results"),
    "SUBMIT_EMPLOYER_RECOMMENDATION": (
        "고용기업 추천서 제출",
        "Submit the employer recommendation letter",
    ),
    "SUBMIT_EVIDENCE": ("증빙 서류 제출", "Submit supporting documents"),
    "RAISE_TOPIK": ("TOPIK {level}급 취득", "Get TOPIK level {level}"),
    "RAISE_INCOME": (
        "최근 2년 평균소득 올리기",
        "Raise your average income for the last 2 years",
    ),
    "BONUS_EMPLOYER_RECOMMENDATION": (
        "고용기업 추천서 받기",
        "Get an employer recommendation letter",
    ),
    "BONUS_GOVERNMENT_RECOMMENDATION": (
        "중앙부처·광역지자체 추천 받기",
        "Get a ministry or provincial government recommendation",
    ),
    "BONUS_QUALIFICATION": ("국내 자격증·학위 취득", "Get a Korean certificate or degree"),
    "BONUS_DRIVER_LICENSE": ("국내 운전면허 취득", "Get a Korean driver's license"),
    "BONUS": ("가점 받기", "Earn bonus points"),
    "FIX_MIN_INCOME": (
        "최근 2년 평균소득 기준 맞추기",
        "Meet the average income requirement for the last 2 years",
    ),
    "FIX_MIN_KOREAN": (
        "한국어 최소 기준 맞추기",
        "Meet the minimum Korean language requirement",
    ),
    "FIX_CONTRACT": (
        "E-7-4 고용계약 조건 맞추기",
        "Meet the E-7-4 employment contract conditions",
    ),
    "FIX_CURRENT_EMPLOYMENT": (
        "현재 근무처에서 계속 일하기",
        "Keep working at your current workplace",
    ),
    "FIX_EMPLOYER_RECOMMENDATION": (
        "고용기업 추천 조건 갖추기",
        "Meet the employer recommendation conditions",
    ),
    "FIX_REQUIREMENT": ("필수 요건 갖추기", "Meet the required conditions"),
    "APPLY": ("E-7-4 전환 신청", "Apply to change to E-7-4"),
}

SENTENCES: dict[str, tuple[str, str]] = {
    "REQUIRED_TAIL": (". 신청하려면 꼭 필요함", ". Required before you apply"),
    "MERGED_WITH_BONUS": (
        "{bonus}: 증빙 서류를 내면 필수요건 충족과 가점 +{points}점이 함께 확정됨. "
        "필수요건: {requirements}",
        "{bonus}: Submitting the documents confirms the required conditions and"
        " +{points} bonus points together. Required conditions: {requirements}",
    ),
    "MERGED": (
        "증빙 서류를 내면 필수요건이 함께 확정됨: {requirements}",
        "Submitting the documents confirms these required conditions together: {requirements}",
    ),
    "APPLY_NEEDS_STAY": (
        "체류기간 정보를 확인하면 신청 가능일을 계산할 수 있음(아래 질문에 답해 주세요)",
        "Once your stay period is confirmed, the date you can apply from can be calculated"
        " (please answer the questions below)",
    ),
    "APPLY_STAY_EXPIRES_FIRST": (
        "신청 가능일({applicable})보다 체류 만료일({expires})이 먼저 옴. "
        "체류기간 연장 여부를 먼저 확인해야 함",
        "Your stay expires ({expires}) before the date you can apply from ({applicable})."
        " Check first whether you can extend your stay",
    ),
    "APPLY_WINDOW": (
        "{applicable}부터 체류 만료일({expires}) 전까지 출입국·외국인관서에 신청",
        "Apply at an immigration office from {applicable} until your stay expires ({expires})",
    ),
    "APPLY_AFTER_REQUIRED": (
        "위 필수 단계를 모두 마친 뒤 {applicable}부터 체류 만료일({expires}) 전까지 "
        "출입국·외국인관서에 신청",
        "After you finish all the required steps above, apply at an immigration office"
        " from {applicable} until your stay expires ({expires})",
    ),
}
# 필수요건 나열 구분자(한국어, 영어). 영어 규칙 설명에는 쉼표가 들어 있어 세미콜론으로 나눈다.
REQUIREMENT_SEPARATORS = (", ", "; ")


def _pick(pair: tuple[str, str], language: str) -> str:
    return pair[0] if is_korean(language) else pair[1]


def _sentence(key: str, language: str, **params: object) -> str:
    return _pick(SENTENCES[key], language).format(**params)


def _title_key(imp: Improvement, rule: Rule) -> tuple[str, dict[str, object]]:
    c = rule.condition
    kind = imp.kind
    if kind is ImprovementKind.SUBMIT_EVIDENCE:
        if rule.category is RuleCategory.BASE_INCOME:
            return "SUBMIT_INCOME", {}
        if rule.category is RuleCategory.BASE_KOREAN:
            return "SUBMIT_KOREAN", {}
        if (
            isinstance(c, EvidencePresent)
            and c.evidence_type is EvidenceType.RECOMMENDATION_EMPLOYER
        ):
            return "SUBMIT_EMPLOYER_RECOMMENDATION", {}
        return "SUBMIT_EVIDENCE", {}
    if kind is ImprovementKind.RAISE_LEVEL:
        if isinstance(c, KoreanLevel):
            return "RAISE_TOPIK", {"level": c.topik_level_min}
        return "RAISE_INCOME", {}
    if kind is ImprovementKind.OBTAIN_BONUS:
        if isinstance(c, EvidencePresent):
            if c.evidence_type is EvidenceType.RECOMMENDATION_EMPLOYER:
                return "BONUS_EMPLOYER_RECOMMENDATION", {}
            return "BONUS_GOVERNMENT_RECOMMENDATION", {}
        if isinstance(c, Qualification):
            return "BONUS_QUALIFICATION", {}
        if isinstance(c, DriverLicense):
            return "BONUS_DRIVER_LICENSE", {}
        return "BONUS", {}
    if isinstance(c, MinAverageIncome):
        return "FIX_MIN_INCOME", {}
    if isinstance(c, MinKorean):
        return "FIX_MIN_KOREAN", {}
    if isinstance(c, Contract):
        return "FIX_CONTRACT", {}
    if isinstance(c, CurrentEmployment):
        return "FIX_CURRENT_EMPLOYMENT", {}
    if isinstance(c, EmployerRecommendation):
        return "FIX_EMPLOYER_RECOMMENDATION", {}
    return "FIX_REQUIREMENT", {}


def _title(imp: Improvement, rule: Rule, language: str = KOREAN) -> str:
    key, params = _title_key(imp, rule)
    return _pick(TITLES[key], language).format(**params)


def improvement_description(imp: Improvement, language: str = KOREAN) -> str:
    """개선 방안 설명. ko가 아니면 descriptionEn, 없으면 한국어."""
    if is_korean(language) or imp.description_en is None:
        return imp.description
    return imp.description_en


def improvement_documents(imp: Improvement, language: str = KOREAN) -> tuple[str, ...]:
    """서류 이름(requiredEvidence). ko가 아니면 documentsEn, 없으면 한국어."""
    if is_korean(language) or imp.documents_en is None:
        return imp.documents
    return imp.documents_en


def rule_description(rule: Rule, language: str = KOREAN) -> str:
    """규칙 설명. ko가 아니면 texts.RULES의 영어, 없으면 한국어."""
    if is_korean(language):
        return rule.description
    return rule_text(rule.rule_code, rule.description).en or rule.description


def template_step_text(
    imp: Improvement, rule: Rule, *, required: bool, language: str = KOREAN
) -> StepText:
    description = improvement_description(imp, language)
    if required and imp.kind is not ImprovementKind.FIX_REQUIREMENT:
        description += _sentence("REQUIRED_TAIL", language)
    return StepText(title=_title(imp, rule, language), description=description)


def template_merged_text(
    members: Sequence[tuple[Improvement, Rule]], *, required: bool, language: str = KOREAN
) -> StepText:
    """같은 서류로 끝나는 방안을 합친 단계(steps.group_steps). 필수요건 충족과 가점을 함께 말한다.

    합친 서류 제출 방안 중 점수가 없는 것은 필수요건이다(엔진의 요건 증빙 제출 방안).
    """
    lead, lead_rule = members[0]
    separator = _pick(REQUIREMENT_SEPARATORS, language)
    requirements = separator.join(
        rule_description(rule, language) for imp, rule in members if imp.points_gain is None
    )
    scored = next(((imp, rule) for imp, rule in members if imp.points_gain is not None), None)
    if scored is not None:
        bonus, bonus_rule = scored
        description = _sentence(
            "MERGED_WITH_BONUS",
            language,
            bonus=rule_description(bonus_rule, language),
            points=bonus.points_gain,
            requirements=requirements,
        )
    else:
        description = _sentence("MERGED", language, requirements=requirements)
    if required:
        description += _sentence("REQUIRED_TAIL", language)
    return StepText(title=_title(lead, lead_rule, language), description=description)


def template_apply_text(
    applicable: date | None,
    expires: date,
    *,
    required_steps_pending: bool = False,
    language: str = KOREAN,
) -> StepText:
    """신청 단계 문장. 필수 단계가 남아 있으면 그 날짜에 바로 신청할 수 있다고 쓰지 않는다."""
    title = _pick(TITLES["APPLY"], language)
    if applicable is None:
        return StepText(title, _sentence("APPLY_NEEDS_STAY", language))
    dates = {"applicable": applicable.isoformat(), "expires": expires.isoformat()}
    if applicable > expires:
        return StepText(title, _sentence("APPLY_STAY_EXPIRES_FIRST", language, **dates))
    if required_steps_pending:
        return StepText(title, _sentence("APPLY_AFTER_REQUIRED", language, **dates))
    return StepText(title, _sentence("APPLY_WINDOW", language, **dates))


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
        "- combinesRequirementAndBonus가 true인 단계는 서류를 한 번 내서 필수요건도 채우고 "
        "가점도 받는 단계예요. description에 '필수요건'과 estimatedAdditionalScore 점수를 "
        "함께 써요.",
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


def _says_both(description: str, points: int) -> bool:
    """합친 단계 설명이 필수요건과 가점 점수를 모두 말하는지("필수 요건" 띄어쓰기 허용)."""
    return "필수요건" in description.replace(" ", "") and points in numbers_in(description)


def check_llm_texts(
    steps: Sequence[dict[str, Any]], texts: dict[int, StepText]
) -> dict[int, StepText]:
    """면책 문장을 지운 뒤, 단계 데이터에 없는 숫자·금지 표현·빠진 단계가 있거나 합친 단계가
    필수요건·가점 중 하나만 말하면 LlmTextError."""
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
    for s in steps:
        if s.get("combinesRequirementAndBonus") and not _says_both(
            texts[s["order"]].description, s["estimatedAdditionalScore"]
        ):
            raise LlmTextError("COMBINED_STEP_TEXT")
    return texts


def rejects_thinking(exc: Exception) -> bool:
    """thinking 설정을 거부한 400인지. 상태 코드와 오류 메시지의 "thinking"으로 판단한다."""
    return getattr(exc, "status_code", None) == 400 and "thinking" in str(exc).lower()


class LlmStepWriter:
    """Claude로 단계 문장을 쓴다. 실패하면 LlmTextError를 올리고 호출 쪽이 템플릿을 쓴다.

    ``timeout_seconds``는 write 한 번 전체의 시간이다. 호출마다 남은 시간만 timeout으로 넘기고,
    클라이언트 자체 재시도는 끄고 쓴다(main._roadmap_writer). 동기 라우트가 스레드풀을 오래
    잡지 않게 하고, Spring이 먼저 포기한 요청에 LLM 비용을 쓰지 않기 위해서다.
    """

    prompt_version = PROMPT_VERSION

    def __init__(
        self,
        client: ClaudeClient,
        model_id: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self.model_id = model_id
        self._timeout = timeout_seconds
        self._clock = clock

    def _create(self, steps: Sequence[dict[str, Any]], deadline: float, **extra: Any) -> Any:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise TimeoutError("roadmap text time budget used up")
        return self._client.messages.create(
            model=self.model_id,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": json.dumps({"steps": steps}, ensure_ascii=False)}
            ],
            output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            timeout=remaining,
            **extra,
        )

    def write(self, steps: Sequence[dict[str, Any]]) -> dict[int, StepText]:
        deadline = self._clock() + self._timeout
        try:
            try:
                # 문장 다듬기라 추론이 필요 없다. 끄지 않으면 모델이 호출마다 thinking 여부를 정해
                # 응답 시간이 2배 이상 흔들린다(측정: 출력 토큰 약 450 ↔ 1,200).
                response = self._create(steps, deadline, thinking={"type": "disabled"})
            except Exception as exc:
                if not rejects_thinking(exc):
                    raise
                if deadline - self._clock() < MIN_RETRY_SECONDS:
                    raise TimeoutError("no time left to retry without thinking") from exc
                # thinking을 끌 수 없는 모델(Opus 5.5, Fable 등): 설정을 빼고 한 번만 다시 부른다.
                logger.warning(
                    "roadmap text LLM rejected thinking=disabled (400) for model=%s; "
                    "retrying once without thinking",
                    self.model_id,
                )
                response = self._create(steps, deadline)
        except Exception as exc:  # 네트워크·인증·한도·재시도 실패 모두 템플릿으로 되돌린다
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

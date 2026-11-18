"""환율 진단 답변 채점(#91): 기간 평균과의 방향, 예측·권유 문장.

답변 글로만 판정한다(API 호출 없음).

- direction_check(answer, fx_contents): 답변의 기간 언급(1·3·6·12개월)마다 같은 문장의 방향 낱말
  (lower·below·cheaper / higher·above / same·in line·similar·close, 한국어 낮·높·비슷)을 찾아
  fx_rate_summary 결과의 currentVsAverage(LOWER·HIGHER·SAME)와 비교한다.
  - 방향 낱말은 기간 언급보다 앞에서 가장 가까운 것을 쓴다("lower than the 1-month average ..., the
    3-month average ..."는 두 기간 모두 lower). 앞에 없으면 뒤에서 가장 가까운 것을 쓴다.
  - 방향 낱말 바로 뒤가 현재 값("the current rate", "today's rate")이면 주어가 평균이라 뒤집는다
    ("the 3-month average sits above the current rate" → 현재가 LOWER).
  - 그 문장에 방향 낱말이 없으면, 바로 다음 문장에 기간 언급이 없을 때만 그 문장에서 찾는다
    ("compared with a 1-year average of ... That is 2.8317 KRW lower").
  - 통화는 기간 언급보다 앞에서 가장 가까운 통화 이름(USD·dollar·PHP·peso 등). 없고 도구 결과 통화가
    하나면 그 통화.
  - 방향 낱말을 못 찾은 언급(noDirection), 통화를 못 정한 언급(unattributed)은 불일치로 치지 않고
    따로 센다. 데이터 부족 기간(insufficientDataPeriods)에 방향을 말하면 불일치로 센다.
- advice_sentences(answer): 앞으로의 방향·송금 시점 권유 표현이 있는 문장(부정문 제외).
  사람이 다시 읽는다.
"""

from __future__ import annotations

import re
from typing import Any

_I = re.IGNORECASE

_PERIODS: tuple[tuple[str, str], ...] = (
    ("1M", r"\b(?:1|one)[- ]month\b|\blast month\b|\bpast month\b|\b1M\b|1개월|한 달|지난달"),
    ("3M", r"\b(?:3|three)[- ]months?\b|\b3M\b|3개월|석 달"),
    ("6M", r"\b(?:6|six)[- ]months?\b|\b6M\b|6개월|반년"),
    (
        "1Y",
        r"\b(?:12|twelve)[- ]months?\b|\b(?:1|one)[- ]year\b|\b(?:past|last|a) year\b"
        r"|\b1Y\b|\bannual\b|12개월|1년|일 년",
    ),
)
_PERIOD = re.compile("|".join(f"(?P<p{k}>{rx})" for k, rx in _PERIODS), _I)
_DIRECTION = re.compile(
    r"(?P<SAME>\babout the same\b|\bthe same as\b|\bin line with\b|\bsimilar to\b|\bclose to\b"
    r"|\bon par with\b|비슷|거의 같)"
    r"|(?P<LOWER>\blower\b|\bbelow\b|\bcheaper\b|\bless than\b|낮)"
    r"|(?P<HIGHER>\bhigher\b|\babove\b|\bmore expensive\b|\bpricier\b|\bmore than\b|높)",
    _I,
)
# 방향 낱말 바로 뒤가 현재 값이면 주어가 평균이다(뒤집는다).
_CURRENT_AFTER = re.compile(
    r"^\s*(?:than\s+)?(?:the\s+)?(?:current|today'?s|latest)\b(?:\s+(?:rate|value|one))?", _I
)
_CURRENCY = re.compile(
    r"(?P<USD>\bUSD\b|\bU\.?S\.? dollars?\b|\bdollars?\b)"
    r"|(?P<PHP>\bPHP\b|\bPhilippine pesos?\b|\bpesos?\b)"
    r"|(?P<VND>\bVND\b|\bVietnamese dong\b|\bdong\b)",
    _I,
)
_FLIP = {"LOWER": "HIGHER", "HIGHER": "LOWER", "SAME": "SAME"}
_SENTENCE = re.compile(r"[^.!?\n]+(?:[.!?]+|$)")
_ADVICE = re.compile(
    r"\b(will (?:likely |probably )?(?:rise|fall|go up|go down|drop|increase|decrease|get cheaper|"
    r"be (?:higher|lower))|is (?:likely|expected) to|expect(?:ed)? to|forecast|"
    r"good time to send|bad time to send|best time|(?:you )?should (?:send|wait)|"
    r"(?:better|wise) to (?:send|wait)|recommend (?:sending|waiting)|send (?:it )?now)\b",
    _I,
)
_NEGATION = re.compile(r"\b(?:can(?:no|')t|cannot|not|unable to|don't|never)\b", _I)


def _sentences(text: str) -> list[tuple[int, str]]:
    """(시작 위치, 문장). 소수점(20.984)·날짜는 문장 끝으로 보지 않는다."""
    protected = re.sub(r"(?<=\d)\.(?=\d)", "\x00", text)
    return [(m.start(), m.group().replace("\x00", ".")) for m in _SENTENCE.finditer(protected)]


def _direction_in(sentence: str, start: int, end: int, *, prefer_before: bool = True) -> str | None:
    """sentence 안 [start, end) 기간 언급에 걸리는 방향. 없으면 None."""
    found = list(_DIRECTION.finditer(sentence))
    before = [m for m in found if m.end() <= start]
    after = [m for m in found if m.start() >= end]
    pick = (before[-1] if before else None) if prefer_before else None
    if pick is None and after:
        pick = after[0]
    if pick is None:
        return None
    kind = pick.lastgroup
    assert kind is not None
    if _CURRENT_AFTER.match(sentence[pick.end() :]):
        kind = _FLIP[kind]
    return kind


def _expected(fx_contents: list[Any]) -> dict[str, dict[str, str | None]]:
    """통화 → 기간 → currentVsAverage(데이터 부족 기간은 None)."""
    out: dict[str, dict[str, str | None]] = {}
    for content in fx_contents:
        for item in (content or {}).get("currencies") or []:
            periods = out.setdefault(item["currency"], {})
            for avg in item.get("periodAverages") or []:
                periods[avg["period"]] = avg.get("currentVsAverage")
            for period in item.get("insufficientDataPeriods") or []:
                periods[period] = None
    return out


def direction_check(answer: str, fx_contents: list[Any]) -> dict[str, Any]:
    expected = _expected(fx_contents)
    only = next(iter(expected)) if len(expected) == 1 else None
    sentences = _sentences(answer)
    mentions: list[dict[str, Any]] = []
    for index, (offset, sentence) in enumerate(sentences):
        for m in _PERIOD.finditer(sentence):
            period = next(k for k, _ in _PERIODS if m.group(f"p{k}"))
            said = _direction_in(sentence, m.start(), m.end())
            if said is None and index + 1 < len(sentences):
                following = sentences[index + 1][1]
                if not _PERIOD.search(following):
                    said = _direction_in(following, 0, 0, prefer_before=False)
            currencies = [c for c in _CURRENCY.finditer(answer[: offset + m.start()])]
            currency = currencies[-1].lastgroup if currencies else only
            want = expected.get(currency or "", {}).get(period, "") if currency else ""
            if currency is None or currency not in expected:
                match = None
                note = "unattributed"
            elif said is None:
                match = None
                note = "noDirection"
            elif want is None:
                match = False
                note = "insufficientData"
            elif want == "":
                match = None
                note = "periodNotInTool"
            else:
                match = said == want
                note = "match" if match else "mismatch"
            mentions.append(
                {
                    "currency": currency,
                    "period": period,
                    "said": said,
                    "expected": want or None,
                    "result": note,
                    "text": sentence.strip()[:160],
                }
            )
    return {
        "mentions": mentions,
        "mismatches": sum(1 for x in mentions if x["result"] in ("mismatch", "insufficientData")),
        "matches": sum(1 for x in mentions if x["result"] == "match"),
        "noDirection": sum(1 for x in mentions if x["result"] == "noDirection"),
        "unattributed": sum(
            1 for x in mentions if x["result"] in ("unattributed", "periodNotInTool")
        ),
    }


def advice_sentences(answer: str) -> list[str]:
    """예측·권유 표현이 있는 문장(부정문 제외). 사람이 다시 읽는다."""
    return [
        s.strip() for _, s in _sentences(answer) if _ADVICE.search(s) and not _NEGATION.search(s)
    ]

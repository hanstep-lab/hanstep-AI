"""금융 정책 답변 위험 문장 검사기(#93, 규칙 v3).

제4-4조 ④(건당 미화 5천불 이내 증빙 없이 **수령**)를 **송금** 허용으로 옮긴 문장을 찾는다.
규칙: ai/specs/rag/finance_policy_answer_guard_v3.md(2·3절), 적용 설계 6절.

- R3 = 한 문장에 M(금액 USD 5,000) ∧ S(송금 방향) ∧ ¬R(수령 방향) ∧ D3(무증빙).
- 신호·D3 정규식은 이 모듈 하나에 둔다. 평가 스크립트(scripts/eval/check_policy_answer_guard.py)가
  여기서 가져다 쓴다(규칙 중복 없음).
- ``SentenceGuard``는 스트리밍 텍스트를 문장 단위로 검사한다. ``replace``는 걸린 문장을
  대체 문장으로 바꾸고(한 답변에 한 번), ``log``는 바꾸지 않고 적중만 모은다.

표준 라이브러리만 쓴다(평가 스크립트가 서버 의존성 없이 import한다).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

_I = re.IGNORECASE

# v1 2절 신호(v3도 그대로 쓴다).
MONEY = [
    re.compile(r"(?<![\d,.])(?:US\$|USD\s?|\$)?\s?5,000(?![\d]|,\d)", _I),
    re.compile(r"(?<![\d,.])(?:US\$|USD\s?|\$)?\s?5000(?!\d)", _I),
    re.compile(r"\b5k\b", _I),
    re.compile(r"\bfive\s+thousand\b", _I),
    re.compile(r"5천\s?(?:달러|불)", _I),
]
SEND = [
    re.compile(r"\b(?:send|sends|sending|sent)\b", _I),
    re.compile(r"\btransfer\w*", _I),
    re.compile(r"\bremit\w*", _I),
    re.compile(r"\bwire\w*", _I),
    re.compile(r"보내"),
    re.compile(r"송금"),
]
RECEIVE = [
    re.compile(r"\breceiv\w*", _I),
    re.compile(r"\bincoming\b", _I),
    re.compile(r"\binbound\b", _I),
    re.compile(r"받"),
    re.compile(r"수령"),
]

# v3 2절 D3: 부정 표현과 서류 단어가 0~3단어 안에(갈래 a·b), 한국어(c).
_DOC = r"(?:documents?|paperwork|proof|evidence)"
_GAP = r"(?:\s+[\w'’-]+){0,3}?\s+"
D3: dict[str, re.Pattern[str]] = {
    "a": re.compile(
        r"\b(?:without|no|do\s+not\s+need|does\s+not\s+need|don['’]t\s+need|doesn['’]t\s+need|"
        r"needn['’]t|no\s+need\s+for)" + _GAP + _DOC + r"\b",
        _I,
    ),
    "b": re.compile(
        r"\b"
        + _DOC
        + _GAP
        + r"(?:not\s+required|not\s+needed|not\s+necessary|are\s+not|is\s+not)\b",
        _I,
    ),
    "c": re.compile(r"(?:증빙|서류)[^\s.]{0,3}\s?(?:없이|없어도|필요\s?없)"),
}

RULE = "v3"
REPLACEMENT = (
    "Under Article 4-4(4) of the Foreign Exchange Transaction Regulation, the no-document "
    "allowance of up to USD 5,000 per transaction applies only to money you receive, not to "
    "money you send abroad."
)
GuardMode = Literal["off", "log", "replace"]
GUARD_MODES: tuple[str, ...] = ("off", "log", "replace")


def any_match(patterns: Iterable[re.Pattern[str]], text: str) -> bool:
    return any(p.search(text) for p in patterns)


def d3_branch(sentence: str) -> str | None:
    """D3 갈래('a'·'b'·'c') 중 먼저 맞는 것. 없으면 None."""
    return next((name for name, p in D3.items() if p.search(sentence)), None)


def has_d3(sentence: str) -> bool:
    return d3_branch(sentence) is not None


def classify_v3(sentence: str) -> str | None:
    """v3: M ∧ S ∧ ¬R ∧ D3 = 'R3'. M ∧ S ∧ R은 'mixed'(참고로만 센다). 그 밖은 None."""
    if not (any_match(MONEY, sentence) and any_match(SEND, sentence)):
        return None
    if any_match(RECEIVE, sentence):
        return "mixed"
    return "R3" if has_d3(sentence) else None


# 6-2절: 이 약어 뒤 마침표에서는 끊지 않는다.
_ABBREV = re.compile(r"(?:\bU\.S|\be\.g|\bi\.e|\bNo|\bArt|\betc)\.$", _I)
_END = re.compile(r"[.!?](?=\s)|\n")


def split_complete(buffer: str) -> tuple[list[str], str]:
    """버퍼를 완성된 조각(문장 + 뒤 공백)들과 남은 텍스트로 나눈다.

    경계: 마침표·물음표·느낌표 뒤 공백, 줄바꿈. 약어 뒤 마침표와 소수점(뒤가 공백이 아님)은
    경계가 아니다. 버퍼 끝의 마침표는 다음 글자를 볼 때까지 남긴다.
    """
    pieces: list[str] = []
    start = 0
    for m in _END.finditer(buffer):
        i = m.start()
        if i < start:
            continue
        if buffer[i] != "\n" and _ABBREV.search(buffer[start : i + 1]):
            continue
        j = i + 1
        while j < len(buffer) and buffer[j].isspace():
            j += 1
        pieces.append(buffer[start:j])
        start = j
    return pieces, buffer[start:]


@dataclass
class GuardHit:
    sentence: str
    branch: str


@dataclass
class SentenceGuard:
    """금융 정책 답변 호출의 텍스트를 문장 단위로 검사한다(6-1절).

    - ``replace``: ``feed``는 완성된 문장만 검사 뒤 돌려주고 나머지는 버퍼에 둔다.
      걸린 문장은 처음 한 번만 대체 문장으로 바꾸고, 그 뒤 걸린 문장은 뺀다.
    - ``log``: ``feed``는 받은 텍스트를 그대로 돌려주고, 같은 텍스트로 문장 검사만 한다.
    - ``finish``는 정상 종료 때만 부른다(남은 버퍼를 검사해 돌려준다).
    """

    mode: GuardMode
    hits: list[GuardHit] = field(default_factory=list)
    replaced: bool = False
    _buffer: str = ""

    @property
    def buffers(self) -> bool:
        return self.mode == "replace"

    def feed(self, text: str) -> str:
        self._buffer += text
        pieces, self._buffer = split_complete(self._buffer)
        checked = "".join(self._check(p) for p in pieces)
        return text if self.mode == "log" else checked

    def finish(self) -> str:
        rest, self._buffer = self._buffer, ""
        checked = self._check(rest) if rest else ""
        return "" if self.mode == "log" else checked

    def _check(self, piece: str) -> str:
        sentence = piece.strip()
        if not sentence or classify_v3(sentence) != "R3":
            return piece
        self.hits.append(GuardHit(sentence, d3_branch(sentence) or ""))
        if self.mode != "replace":
            return piece
        trail = piece[len(piece.rstrip()) :]
        if self.replaced:
            return "\n" if "\n" in trail else ""
        self.replaced = True
        lead = piece[: len(piece) - len(piece.lstrip())]
        return lead + REPLACEMENT + trail

    def metrics(self) -> dict[str, Any]:
        """agent.metrics용(측정 전용). 대체 전 원문 문장을 담으므로 로그·DB에 넣지 않는다."""
        return {
            "rule": RULE,
            "mode": self.mode,
            "buffered": self.buffers,
            "hits": len(self.hits),
            "branches": [h.branch for h in self.hits],
            "replaced": self.replaced,
            "sentences": [h.sentence for h in self.hits],
        }

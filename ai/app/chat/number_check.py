"""Numbers in a chat answer vs. numbers the answer may use. Measurement only (evaluate_chat).

Not part of the response flow. The answer text (no thinking) is scanned for numbers with
thousands separators, decimals, %, currencies (KRW, ₩, won, 원, VND), Korean units (천·만·억),
"약"/"about" and English k/million. Allowed numbers come from the tool result (numeric
values and numbers in its strings) and from the question or history texts. The year, month
and day of dates found there (periodTo 2026-10-31 -> 2026, 10, 31) are kept apart: they back
dates, ordinals and unit-less numbers ("October 31") only, never an amount or a percent, so
"31%" or "KRW 31" is unsupported. An amount or unit-less number is also supported when it
equals an allowed value (dates excluded) rounded half-up to 1,000 or 10,000 won ("약 68만 원"
for 675,317). Dates and ordinals are reported apart as ambiguous when not plainly supported.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

_DATE = re.compile(
    r"(?P<y>\d{4})[-./](?P<m>\d{1,2})(?:[-./](?P<d>\d{1,2}))?"
    r"|(?P<ky>\d{4})\s*년(?:\s*(?P<km>\d{1,2})\s*월)?(?:\s*(?P<kd>\d{1,2})\s*일)?"
    r"|(?P<km2>\d{1,2})\s*월(?:\s*(?P<kd2>\d{1,2})\s*일)?"
)
_ORDINAL = re.compile(r"\b\d+(?:st|nd|rd|th)\b|^\s*\d+[.)]\s", re.MULTILINE)
_KOREAN = re.compile(
    r"(?P<approx>약\s*)?"
    r"(?=\d[\d,.]*\s*[억만천])"
    r"(?:(?P<eok>\d[\d,.]*)\s*억)?\s*(?:(?P<man>\d[\d,.]*)\s*만)?\s*(?:(?P<cheon>\d[\d,.]*)\s*천)?"
    r"\s*(?P<won>원)?"
)
_NUMBER = re.compile(
    r"(?P<approx>(?:about|around|approximately|약)\s*|~\s*)?"
    r"(?P<cur>KRW|₩|VND)?\s*"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s*(?P<unit>million|mn|k|K)\b)?"
    r"(?P<pct>\s*(?:%|퍼센트|percent))?"
    r"(?P<cur2>\s*(?:KRW|won|원|VND))?",
    re.IGNORECASE,
)
_UNITS = {"million": Decimal(1_000_000), "mn": Decimal(1_000_000), "k": Decimal(1000)}


@dataclass
class FoundNumber:
    text: str
    value: str
    kind: str  # amount | percent | number | date | ordinal
    status: str  # supported | unsupported | ambiguous


@dataclass
class NumberCheck:
    found: list[FoundNumber] = field(default_factory=list)

    @property
    def unsupported(self) -> list[str]:
        return [f.text for f in self.found if f.status == "unsupported"]

    @property
    def ambiguous(self) -> list[str]:
        return [f.text for f in self.found if f.status == "ambiguous"]

    def to_json(self) -> dict[str, Any]:
        return {
            "found": [asdict(f) for f in self.found],
            "unsupported": self.unsupported,
            "ambiguous": self.ambiguous,
        }


def _dec(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


def _norm(value: Decimal) -> str:
    return format(value.normalize(), "f")


def scan_numbers(text: str) -> list[tuple[str, Decimal | None, str, list[Decimal]]]:
    """(matched text, value, kind, date parts) in order of appearance."""
    items: list[tuple[int, str, Decimal | None, str, list[Decimal]]] = []
    taken = [False] * len(text)

    def free(start: int, end: int) -> bool:
        return not any(taken[start:end])

    def take(start: int, end: int) -> None:
        for i in range(start, end):
            taken[i] = True

    for m in _DATE.finditer(text):
        parts = [m.group(g) for g in ("y", "m", "d", "ky", "km", "kd", "km2", "kd2")]
        values = [Decimal(p) for p in parts if p]
        items.append((m.start(), m.group().strip(), None, "date", values))
        take(m.start(), m.end())
    for m in _ORDINAL.finditer(text):
        if free(m.start(), m.end()):
            number = re.search(r"\d+", m.group())
            assert number is not None
            items.append((m.start(), m.group().strip(), None, "ordinal", [Decimal(number.group())]))
            take(m.start(), m.end())
    for m in _KOREAN.finditer(text):
        if not m.group().strip() or not free(m.start(), m.end()):
            continue
        if not any(m.group(g) for g in ("eok", "man", "cheon")):
            continue
        value = Decimal(0)
        for group, scale in (("eok", 100_000_000), ("man", 10_000), ("cheon", 1000)):
            if m.group(group):
                value += _dec(m.group(group)) * scale
        items.append((m.start(), m.group().strip(), value, "amount", []))
        take(m.start(), m.end())
    for m in _NUMBER.finditer(text):
        start = m.start("num")
        if not free(start, m.end("num")):
            continue
        value = _dec(m.group("num"))
        unit = (m.group("unit") or "").lower()
        if unit:
            value *= _UNITS[unit]
        if m.group("pct"):
            kind = "percent"
        elif m.group("cur") or m.group("cur2") or unit:
            kind = "amount"
        else:
            kind = "number"
        items.append((m.start(), m.group().strip(), value, kind, []))
        take(m.start(), m.end())
    items.sort(key=lambda item: item[0])
    return [(t, v, k, p) for _, t, v, k, p in items]


def _walk(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)
    else:
        yield value


@dataclass
class AllowedNumbers:
    values: set[Decimal] = field(default_factory=set)  # back every kind of number
    date_parts: set[Decimal] = field(default_factory=set)  # dates, ordinals, unit-less only


def allowed_numbers(tool_results: Iterable[Any], texts: Iterable[str]) -> AllowedNumbers:
    """Numbers in tool results and in the question/history texts, date parts kept apart."""
    allowed = AllowedNumbers()
    for leaf in _walk(list(tool_results)):
        if isinstance(leaf, bool) or leaf is None:
            continue
        if isinstance(leaf, int | float):
            allowed.values.add(Decimal(str(leaf)))
        elif isinstance(leaf, str):
            texts = [*texts, leaf]
    for text in texts:
        for _, value, kind, parts in scan_numbers(text):
            if value is not None:
                allowed.values.add(value)
            (allowed.date_parts if kind == "date" else allowed.values).update(parts)
    return allowed


def rounded_values(values: set[Decimal]) -> set[Decimal]:
    """Each value rounded half-up to 1,000 and 10,000 (when at least that large)."""
    out: set[Decimal] = set()
    for value in values:
        for step in (Decimal(1000), Decimal(10_000)):
            if abs(value) >= step:
                out.add((value / step).quantize(Decimal(1), rounding=ROUND_HALF_UP) * step)
    return out


def check_answer(answer: str, tool_results: Iterable[Any], texts: Iterable[str]) -> NumberCheck:
    allowed = allowed_numbers(tool_results, texts)
    with_dates = allowed.values | allowed.date_parts
    approx = rounded_values(allowed.values)
    result = NumberCheck()
    for text, value, kind, parts in scan_numbers(answer):
        if kind in ("date", "ordinal"):
            status = "supported" if parts and all(p in with_dates for p in parts) else "ambiguous"
            shown = "-".join(_norm(p) for p in parts)
        else:
            assert value is not None
            exact = value in (with_dates if kind == "number" else allowed.values)
            near = kind != "percent" and value in approx
            status = "supported" if exact or near else "unsupported"
            shown = _norm(value)
        result.found.append(FoundNumber(text=text, value=shown, kind=kind, status=status))
    return result

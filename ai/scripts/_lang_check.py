"""답변 언어 검사(#106): 사용자 언어(preferred_language)와 다른 글자가 답변에 섞였는지 센다.

측정 전용(서버는 쓰지 않는다). 기준은 specs/chat/answer_language_v1_design.md 2절.

- 한글 덩어리: 한글 글자가 이어진 부분. 사이에 공백·가운뎃점 하나가 있으면 한 덩어리로 본다
  ("외국환거래법 시행령"). 덩어리마다 허용 목록(specs/chat/answer_language_allowlist_v1.json)과
  사용자 질문의 한글로 허용 여부를 정한다.
- 허용 판정: 허용 용어(공식 문서 이름) 또는 상품 카탈로그의 상품명과 같거나 그 일부이면 허용.
  조사가 붙은 덩어리("외국환거래규정에")는 허용하지 않는다(한국어 문장의 일부이므로).
- en: 허용 안 된 한글 덩어리가 하나라도 있으면 혼입(mixed). 글자 중 한글 비율이 0.5 이상이면
  본문 대부분 한글(mostlyKorean). 허용 목록 전 값(mixedRaw)도 같이 남긴다.
- 병기 제외 혼입(mixedBare): 같은 문장 안 괄호·큰따옴표 안의 한글, 또는 바로 뒤 괄호에 영어가
  붙은 한글("외국인거주자 (foreign resident)")은 뺀 혼입.
- vi: 혼입이 없고 베트남어 글자(성조·모음 부호) 비율이 라틴 글자의 0.05 이상이면 그 언어로 답함.
- ko: 한글 비율 0.5 이상이면 그 언어로 답함. zh: 한자 비율 0.3 이상.

표준 라이브러리만 쓴다(compare_chat_versions.py가 서버 의존성 없이 import한다).
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

AI_ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = AI_ROOT / "specs" / "chat" / "answer_language_allowlist_v1.json"

_HANGUL = "ᄀ-ᇿ㄰-㆏가-힣"
HANGUL_RUN = re.compile(rf"[{_HANGUL}]+(?:[ ·ㆍ][{_HANGUL}]+)*")
_HANGUL_CHAR = re.compile(rf"[{_HANGUL}]")
_CJK_CHAR = re.compile(r"[一-鿿]")
# 베트남어에서만 쓰는 글자(성조·모음 부호). 영어 답변에는 거의 나오지 않는다.
_VI_CHARS = frozenset(
    "ăâđêôơưĂÂĐÊÔƠƯ"
    "àáảãạằắẳẵặầấẩẫậèéẻẽẹềếểễệìíỉĩịòóỏõọồốổỗộờớởỡợùúủũụừứửữựỳýỷỹỵ"
    "ÀÁẢÃẠẰẮẲẴẶẦẤẨẪẬÈÉẺẼẸỀẾỂỄỆÌÍỈĨỊÒÓỎÕỌỒỐỔỖỘỜỚỞỠỢÙÚỦŨỤỪỨỬỮỰỲÝỶỸỴ"
)
MOSTLY_KOREAN = 0.5
VI_MIN_SHARE = 0.05
ZH_MIN_SHARE = 0.3


def _norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


@lru_cache(maxsize=4)
def allowed_terms(path: Path = ALLOWLIST) -> tuple[str, ...]:
    """허용 용어 + 상품명(카탈로그 nameKo). 공백은 하나로 맞춘다."""
    spec = json.loads(path.read_text(encoding="utf-8"))
    terms = [t for group in spec["terms"].values() for t in group]
    catalog = AI_ROOT / spec["productNamesFrom"]
    for product in json.loads(catalog.read_text(encoding="utf-8"))["products"]:
        name = product.get("nameKo")
        value = name.get("value") if isinstance(name, Mapping) else name
        if isinstance(value, str):
            terms.append(value)
    return tuple(dict.fromkeys(_norm(t) for t in terms))


# 병기로 보는 묶음 기호. 작은따옴표는 영어 축약(don't)과 섞여 쓰지 않는다.
_PAIRS = {"(": ")", "（": "）", "[": "]", "「": "」", "“": "”", "‘": "’"}
_SENTENCE_END = re.compile(r"[.!?\n](?=\s|$)")
_ENGLISH_AFTER = re.compile(rf"[ \t]?\((?=[^()]*[A-Za-z])[^(){_HANGUL}]+\)")


def hangul_runs(text: str) -> list[str]:
    return [_norm(m.group()) for m in HANGUL_RUN.finditer(unicodedata.normalize("NFC", text))]


def _glossed(text: str, start: int, end: int) -> bool:
    """덩어리가 같은 문장 안의 괄호·따옴표 안에 있는지(영어 뒤에 원어를 병기한 경우)."""
    ends_before = [m.end() for m in _SENTENCE_END.finditer(text, 0, start)]
    head = text[ends_before[-1] if ends_before else 0 : start]
    found = _SENTENCE_END.search(text, end)
    tail = text[end : found.start() + 1 if found else len(text)]
    for opening, closing in _PAIRS.items():
        if head.count(opening) > head.count(closing) and closing in tail:
            return True
    if head.count('"') % 2 == 1 and '"' in tail:
        return True
    # 반대 순서 병기: 한글 바로 뒤 괄호 안에 영어("외국인거주자 (foreign resident)").
    return bool(_ENGLISH_AFTER.match(text, end))


def _is_allowed(run: str, terms: Sequence[str], context: str) -> bool:
    if run in context:
        return True
    return any(run in term for term in terms)


def _letters(text: str) -> tuple[int, int, int, int]:
    """(한글, 라틴, 베트남어 전용, 한자) 글자 수."""
    hangul = latin = vi = cjk = 0
    for ch in unicodedata.normalize("NFC", text):
        if _HANGUL_CHAR.match(ch):
            hangul += 1
        elif _CJK_CHAR.match(ch):
            cjk += 1
        elif ch.isalpha() and (ch.isascii() or ch in _VI_CHARS or "À" <= ch <= "ɏ"):
            latin += 1
            if ch in _VI_CHARS:
                vi += 1
    return hangul, latin, vi, cjk


def language_check(
    answer: str,
    language: str,
    context_texts: Iterable[str] = (),
    *,
    terms: Sequence[str] | None = None,
) -> dict[str, Any]:
    """답변 하나의 언어 검사. context_texts(사용자 질문 등)에 있는 한글은 허용한다."""
    terms = allowed_terms() if terms is None else terms
    context = _norm(" ".join(context_texts))
    text = unicodedata.normalize("NFC", answer)
    matches = [(_norm(m.group()), m.start(), m.end()) for m in HANGUL_RUN.finditer(text)]
    runs = [run for run, _, _ in matches]
    disallowed = [run for run, _, _ in matches if not _is_allowed(run, terms, context)]
    bare = [
        run
        for run, start, end in matches
        if not _is_allowed(run, terms, context) and not _glossed(text, start, end)
    ]
    hangul, latin, vi, cjk = _letters(answer)
    letters = hangul + latin + cjk
    hangul_share = hangul / letters if letters else 0.0
    vi_share = vi / latin if latin else 0.0
    cjk_share = cjk / letters if letters else 0.0
    mixed = bool(disallowed)
    if language == "ko":
        in_language = hangul_share >= MOSTLY_KOREAN
    elif language == "vi":
        in_language = not mixed and vi_share >= VI_MIN_SHARE
    elif language == "zh":
        in_language = not mixed and cjk_share >= ZH_MIN_SHARE
    else:
        in_language = not mixed
    return {
        "language": language,
        "mixedRaw": bool(runs),
        # ko 답변에서는 한글이 정상이라 혼입을 세지 않는다.
        "mixed": mixed if language != "ko" else False,
        # 괄호·따옴표 안 병기("designated bank (지정거래외국환은행)")를 뺀 혼입. 설계 2-1.
        "mixedBare": bool(bare) if language != "ko" else False,
        "mostlyKorean": hangul_share >= MOSTLY_KOREAN,
        "inLanguage": in_language,
        "hangulShare": round(hangul_share, 3),
        "viShare": round(vi_share, 3),
        "hangulRuns": runs,
        "disallowedRuns": disallowed if language != "ko" else [],
        "bareRuns": bare if language != "ko" else [],
    }


def row_check(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """행에 저장된 검사(없으면 답변으로 다시 계산, 언어를 모르면 en). 완료 회차만."""
    if row.get("status") != "done":
        return None
    saved = row.get("languageCheck")
    if saved:
        return dict(saved)
    follow = row.get("followUp") or {}
    answer = "\n\n".join(a for a in (row.get("answer") or "", follow.get("answer") or "") if a)
    return language_check(answer, row.get("language") or "en", row.get("contextTexts") or [])


def _ratio(values: Sequence[bool]) -> str:
    return f"{sum(values)}/{len(values)}" if values else "-"


def summary_lines(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """한 설정의 언어 검사 요약(문항별 표 + 걸린 한글 덩어리). 완료 회차만 센다."""
    checked = [(r, c) for r in rows if (c := row_check(r)) is not None and not r.get("warmup")]
    if not checked:
        return []
    out = ["", "### 답변 언어(#106)", ""]
    for language in dict.fromkeys(c["language"] for _, c in checked):
        mine = [c for _, c in checked if c["language"] == language]
        out.append(
            f"- 사용자 언어 {language}: 한글 혼입(허용 목록 적용) "
            f"{_ratio([c['mixed'] for c in mine])}, 괄호·따옴표 병기 제외 "
            f"{_ratio([c.get('mixedBare', c['mixed']) for c in mine])}, 한글 있음(허용 목록 전) "
            f"{_ratio([c['mixedRaw'] for c in mine])}, 본문 대부분 한글 "
            f"{_ratio([c['mostlyKorean'] for c in mine])}, 그 언어로 답함 "
            f"{_ratio([c['inLanguage'] for c in mine])}"
        )
    out += [
        "",
        "| 문항 | 언어 | 회차 | 혼입 | 병기 제외 혼입 | 대부분 한글 | 그 언어로 답함 "
        "| 걸린 한글(회차 수) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    keys = dict.fromkeys((r["questionId"], c["language"]) for r, c in checked)
    for qid, language in keys:
        mine = [c for r, c in checked if r["questionId"] == qid and c["language"] == language]
        runs = Counter(run for c in mine for run in dict.fromkeys(c["disallowedRuns"]))
        shown = ", ".join(f"{run} {n}" for run, n in runs.most_common(5)) or "-"
        out.append(
            f"| {qid} | {language} | {len(mine)} | {_ratio([c['mixed'] for c in mine])} | "
            f"{_ratio([c.get('mixedBare', c['mixed']) for c in mine])} | "
            f"{_ratio([c['mostlyKorean'] for c in mine])} | "
            f"{_ratio([c['inLanguage'] for c in mine])} | {shown} |"
        )
    return out

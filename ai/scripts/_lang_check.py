"""답변 언어 검사(#106): 사용자 언어(preferred_language)와 다른 글자가 답변에 섞였는지 센다.

측정 전용(서버는 쓰지 않는다). 기준은 specs/chat/answer_language_v1_design.md 2절.

- 한글 덩어리: 한글 글자가 이어진 부분. 사이에 공백·가운뎃점 하나가 있으면 한 덩어리로 본다
  ("외국환거래법 시행령"). 덩어리마다 허용 목록(specs/chat/answer_language_allowlist_v1.json)과
  사용자 질문의 한글로 허용 여부를 정한다.
- 허용 판정: 허용 용어(공식 문서 이름) 또는 상품 카탈로그의 상품명과 같거나 그 일부이면 허용.
  조사가 붙은 덩어리("외국환거래규정에")는 허용하지 않는다(한국어 문장의 일부이므로).
- en: 허용 안 된 한글 덩어리가 하나라도 있으면 혼입(mixed). 글자 중 한글 비율이 0.5 이상이면
  본문 대부분 한글(mostlyKorean). 허용 목록 전 값(mixedRaw)도 같이 남긴다.
- 병기 제외 혼입(mixedBare): 괄호 안이 한글 용어 하나뿐이고(한글 20자 이하) 조사·문장 끝이
  없을 때만 병기로 보고 뺀 혼입. 따옴표 안, 한글 뒤 괄호에 영어("외국인거주자 (foreign resident)")는
  병기로 보지 않는다(2026-11-19 확정).
- LE03 보조 지표(improvementCheck): 개선 방안(한국어 능력·점수 증가·필요 서류) 언급 여부.
  en 답변만 자동 판정하고, 사람 확인용 표를 요약 .md에 남긴다.
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


# 병기로 보는 괄호(2026-11-19 확정, 설계 2-1). 따옴표·반대 순서 병기는 인정하지 않는다.
_PAIRS = {"(": ")", "（": "）", "[": "]", "「": "」"}
GLOSS_MAX_HANGUL = 20
# 조사·문장 끝: 낱말(공백으로 나눈 것) 하나라도 이것으로 끝나면 병기가 아니라 혼입이다.
# 명사 끝과 겹치는 한 글자(가·도·로·요 등)는 넣지 않는다(예: 한도, 필요).
_PARTICLE_ENDINGS = (
    "은", "는", "을", "를", "에", "의",
    "에서", "으로", "에게", "까지", "부터",
    "니다", "한다", "된다", "있다", "없다", "이다", "세요", "해요", "어요", "아요", "네요",
)  # fmt: skip


def hangul_runs(text: str) -> list[str]:
    return [_norm(m.group()) for m in HANGUL_RUN.finditer(unicodedata.normalize("NFC", text))]


def _glossed(text: str, start: int, end: int) -> bool:
    """괄호 안이 이 한글 덩어리 하나뿐이고(앞뒤 공백만 허용), 한글 20자 이하이며
    조사·문장 끝이 없을 때만 병기로 본다(예: "designated bank (지정거래외국환은행)")."""
    before = text[:start].rstrip()
    after = text[end:].lstrip()
    if not before or not after or _PAIRS.get(before[-1]) != after[0]:
        return False
    run = text[start:end]
    if len(_HANGUL_CHAR.findall(run)) > GLOSS_MAX_HANGUL:
        return False
    return not any(word.endswith(_PARTICLE_ENDINGS) for word in run.split())


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


# --- LE03 보조 지표(#106 결정 3): 개선 방안 언급 -------------------------------------------
# B(v4.5b)는 improvements[].description을 빼므로, 개선 방법 질문에 방안을 말했는지 따로 본다.
# 자동 판정은 en 답변만(영어 정규식). 다른 언어는 None으로 두고 사람이 표에서 본다.
IMPROVEMENT_PATTERNS = {
    "korean": re.compile(
        r"\bTOPIK\b|\bKIIP\b|social integration|\bKorean[- ](?:language|level|skills?|test|"
        r"proficiency|score)\b|Korean ability",
        re.IGNORECASE,
    ),
    "points": re.compile(
        r"\+\s?\d+\s*(?:points?|pts)\b|\b\d+\s+(?:more|extra|additional)\s+points?\b"
        r"|\b(?:gain|earn|add|raise|increase|boost|improve)\w*\b[^.\n]{0,60}?\bpoints?\b",
        re.IGNORECASE,
    ),
    "documents": re.compile(
        r"\b(?:documents?|certificates?|proof|evidence|paperwork|submit\w*)\b", re.IGNORECASE
    ),
}


def improvement_check(answer: str, language: str) -> dict[str, Any]:
    """개선 방안(한국어 능력·점수 증가·필요 서류 중 하나 이상) 언급. en만 자동 판정."""
    if language != "en":
        return {"auto": False, "mentioned": None, **dict.fromkeys(IMPROVEMENT_PATTERNS)}
    found = {name: bool(p.search(answer)) for name, p in IMPROVEMENT_PATTERNS.items()}
    return {"auto": True, "mentioned": any(found.values()), **found}


def _mark(value: bool | None) -> str:
    return "-" if value is None else ("O" if value else "X")


def improvement_lines(rows: Sequence[Mapping[str, Any]], *, answers: bool) -> list[str]:
    """LE03 보조 지표 요약과 사람 확인용 표(answers=True일 때만 답변 원문)."""
    mine = [r for r in rows if r.get("improvementCheck") and not r.get("warmup")]
    if not mine:
        return []
    auto = [r["improvementCheck"]["mentioned"] for r in mine if r["improvementCheck"]["auto"]]
    out = [
        "",
        "### 개선 방안 언급(LE03 보조 지표, #106)",
        "",
        f"- 자동 판정(en): 언급 {_ratio([bool(v) for v in auto])}, "
        f"자동 판정 안 함(en 아님) {len(mine) - len(auto)}",
    ]
    if not answers:
        return [*out, "- 사람 확인용 표는 요약 .md 파일에만 남긴다."]
    out += [
        "",
        "| 회차 | 언어 | 자동 | 한국어 능력 | 점수 증가 | 필요 서류 | 사람 확인 | 답변 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in mine:
        c = r["improvementCheck"]
        answer = " ".join((r.get("answer") or "(빈 답변)").split()).replace("|", "\\|")
        out.append(
            f"| {r['variant']} {r['questionId']}#{r['repeat']} | {r.get('language') or 'en'} | "
            f"{_mark(c['mentioned'])} | {_mark(c['korean'])} | {_mark(c['points'])} | "
            f"{_mark(c['documents'])} |  | {answer} |"
        )
    return out

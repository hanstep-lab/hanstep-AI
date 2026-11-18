"""금융 정책 문서(해외송금 규정, 이슈 #88) 말뭉치: 추출과 적재 범위 단위.

저장 방식 B(별도 도메인, 메모리 색인)의 입력이다. 문서 목록·적재 범위는
specs/rag/finance_policy_corpus_v1.json에서 읽는다.

- 법령(.hwp)은 국가법령정보센터가 내보낸 HWPML(XML)이다. 문단(P)의 글자(CHAR)를 줄로 모은다.
  현행 조문 뒤에 같은 번호의 시행 예정 판이 이어지고 그 끝에 "[시행일: D] 제n조" 줄이 붙는다.
  그 줄이 붙은 조문은 시행 예정 판으로 보고 색인에 넣지 않는다. '부칙' 뒤의 조문은 본문이 아니다.
- 규정(PDF)은 운영 extractors(pypdf)로 뽑고 쪽 머리말·꼬리말을 지운 뒤 "제n-n조(제목)"로 나눈다.
- 가이드북·보도자료(PDF)는 정해진 쪽만 남기고 소제목 줄에서 나눈다.

단위 하나 = 조문 하나(법령·규정) 또는 소제목 하나(가이드북·보도자료).
단위마다 effective_from을 둔다.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_AI_ROOT = Path(__file__).resolve().parents[2]
CORPUS_PATH = _AI_ROOT / "specs" / "rag" / "finance_policy_corpus_v1.json"

_WS = re.compile(r"\s+")
LAW_ARTICLE = re.compile(r"^\s*(제\d+조(?:의\d+)?)\s*\(([^)]*)\)")
SCHEDULED_MARK = re.compile(r"^\s*\[시행일:\s*([\d.\s]+)\]\s*(.+)$")
ADDENDA = re.compile(r"^\s*부\s*칙")
RULE_ARTICLE = re.compile(r"^\s*(제\d+-\d+조(?:의\d+)?)\s*\(([^)]{1,40})\)")
RULE_PAGE_FURNITURE = re.compile(r"^외국환거래규정$|^법제처\s*-\d+/\s*\d+\s*-\s*국가법령정보센터$")
RULE_NEXT_CHAPTER = re.compile(r"\n제\s*\d+\s*장")


class CorpusError(ValueError):
    """말뭉치 파일이나 적재 범위를 쓸 수 없다."""


def match_key(text: str) -> str:
    """공백·줄바꿈을 모두 지운 문자열(근거 인용 대조용)."""
    return _WS.sub("", text)


@dataclass(frozen=True)
class Unit:
    """적재 범위의 단위 하나(조문 또는 소제목)."""

    doc: str
    unit_id: str  # 예: "REG:제4-4조", "GUIDE:p109:9"
    title: str
    text: str
    pages: tuple[int, ...] = ()
    effective_from: str | None = None
    scheduled: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def ref(self) -> str:
        """질문 세트 gold의 unit 표기(조문 번호 또는 'p<쪽>' 첫 쪽)."""
        if self.doc in ("GUIDE", "PRESS"):
            return f"p{self.pages[0]}" if self.pages else ""
        return self.unit_id.split(":")[1]


def load_manifest(path: Path = CORPUS_PATH) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def hwpml_lines(raw: str) -> list[str]:
    """HWPML 문서의 본문 문단을 줄 목록으로. 빈 문단은 뺀다."""
    raw = raw.replace("&nbsp;", "&#160;")
    raw = re.sub(r"<!DOCTYPE[^\[]*\[.*?\]>", "", raw, flags=re.S)
    root = ET.fromstring(raw)
    body = root.find("BODY")
    if body is None:
        raise CorpusError("HWPML has no BODY")
    lines = []
    for p in body.iter("P"):
        text = "".join(c.text or "" for c in p.iter("CHAR")).replace("\xa0", " ")
        if text.strip():
            lines.append(text.rstrip())
    return lines


@dataclass
class LawArticle:
    article: str
    title: str
    lines: list[str]
    addenda: bool
    scheduled_from: str | None = None


def split_law(lines: Sequence[str]) -> list[LawArticle]:
    """법령 줄을 조문으로 나누고, 시행일 표시가 붙은 조문(시행 예정 판)을 표시한다."""
    articles: list[LawArticle] = []
    current: LawArticle | None = None
    in_addenda = False
    for line in lines:
        if ADDENDA.match(line):
            in_addenda = True
        m = LAW_ARTICLE.match(line)
        if m:
            current = LawArticle(m.group(1), m.group(2), [line], in_addenda)
            articles.append(current)
            continue
        if current is None:
            continue
        mark = SCHEDULED_MARK.match(line)
        if mark and mark.group(2).strip().startswith(current.article):
            current.scheduled_from = _WS.sub("", mark.group(1)).rstrip(".")
        current.lines.append(line)
    return articles


def _iso(dotted: str) -> str:
    y, m, d = (int(x) for x in dotted.split("."))
    return f"{y:04d}-{m:02d}-{d:02d}"


def _only_item(text: str, item: str) -> str:
    """조문에서 '<item>.' 호만 남긴다(다음 호 전까지). 머리 줄(조 번호·제목)은 둔다."""
    head = text.splitlines()[0]
    # 다음 호 번호 뒤에 공백이 없을 수 있다(규정 PDF는 띄어쓰기가 사라짐: '36."채권…').
    m = re.search(rf"\n\s*{item}\.\s*.*?(?=\n\s*{int(item) + 1}\.|\Z)", text, re.S)
    if not m:
        raise CorpusError(f"item {item} not found in {head[:20]}")
    title = LAW_ARTICLE.match(head) or RULE_ARTICLE.match(head)
    first = f"{title.group(1)}({title.group(2)})" if title else head
    return f"{first}\n{m.group(0).strip()}"


def law_units(doc: dict[str, Any], lines: Sequence[str]) -> tuple[list[Unit], list[Unit]]:
    """(현행 단위, 시행 예정 판 단위). 적재 범위 조문만."""
    current: list[Unit] = []
    scheduled: list[Unit] = []
    items = doc.get("items") or {}
    for art in split_law(lines):
        if art.addenda or art.article not in doc["articles"]:
            continue
        text = "\n".join(art.lines)
        if art.article in items:
            text = _only_item(text, items[art.article])
        unit = Unit(
            doc=doc["key"],
            unit_id=f"{doc['key']}:{art.article}" + (":scheduled" if art.scheduled_from else ""),
            title=art.title,
            text=text,
            effective_from=_iso(art.scheduled_from) if art.scheduled_from else doc["effectiveFrom"],
            scheduled=bool(art.scheduled_from),
        )
        (scheduled if unit.scheduled else current).append(unit)
    return current, scheduled


def rule_units(doc: dict[str, Any], pages: dict[int, str]) -> list[Unit]:
    """규정 PDF: 머리말·꼬리말을 지우고 조문으로 나눈 뒤 적재 범위만."""
    lines: list[tuple[int, str]] = []
    for number in sorted(pages):
        for line in pages[number].splitlines():
            if not RULE_PAGE_FURNITURE.match(line.strip()):
                lines.append((number, line))
    raw: list[dict[str, Any]] = []
    for number, line in lines:
        m = RULE_ARTICLE.match(line)
        if m:
            raw.append({"article": m.group(1), "title": m.group(2), "lines": [], "pages": []})
        if raw:
            raw[-1]["lines"].append(line)
            if number not in raw[-1]["pages"]:
                raw[-1]["pages"].append(number)
    items = doc.get("items") or {}
    notes = doc.get("notes") or {}
    units = []
    for art in raw:
        if art["article"] not in doc["articles"]:
            continue
        text = RULE_NEXT_CHAPTER.split("\n".join(art["lines"]))[0]
        pages_used = tuple(art["pages"])
        if art["article"] in items:
            text = _only_item(text, items[art["article"]])
            item_key = match_key(text.splitlines()[1])[:20]
            pages_used = (
                tuple(n for n in art["pages"] if item_key in match_key(pages.get(n, "")))
                or pages_used
            )
        meta = {"note": notes[art["article"]]} if art["article"] in notes else {}
        units.append(
            Unit(
                doc["key"],
                f"{doc['key']}:{art['article']}",
                art["title"],
                text,
                pages_used,
                doc["effectiveFrom"],
                meta=meta,
            )  # fmt: skip
        )
    missing = set(doc["articles"]) - {u.unit_id.split(":")[1] for u in units}
    if missing:
        raise CorpusError(f"{doc['key']}: articles not found {sorted(missing)}")
    return units


def section_units(doc: dict[str, Any], pages: dict[int, str]) -> list[Unit]:
    """가이드북·보도자료: 정해진 쪽만, 소제목 줄에서 나눈다."""
    heading = re.compile(doc["headingPattern"])
    raw: list[dict[str, Any]] = []
    for number in doc["pages"]:
        for line in pages.get(number, "").splitlines():
            if heading.match(line) or not raw:
                raw.append({"title": line.strip()[:80], "lines": [], "pages": [], "first": number})
            raw[-1]["lines"].append(line)
            if number not in raw[-1]["pages"]:
                raw[-1]["pages"].append(number)
    units = []
    for i, sec in enumerate(raw):
        text = "\n".join(sec["lines"])
        if match_key(text):
            units.append(
                Unit(
                    doc["key"],
                    f"{doc['key']}:p{sec['first']}:{i}",
                    sec["title"],
                    text,
                    tuple(sec["pages"]),
                    doc.get("issuedOn"),
                )  # fmt: skip
            )
    return units


def pdf_pages(data: bytes) -> dict[int, str]:
    """운영 extractors(pypdf)로 쪽별 텍스트."""
    from app.rag.extractors import extract

    report = extract("pdf", data)
    if report.status != "ok":
        raise CorpusError(f"pdf extraction {report.status}: {report.reason}")
    pages: dict[int, list[str]] = {}
    for block in report.blocks:
        pages.setdefault(block.page_number or 0, []).append(block.text)
    return {k: "\n".join(v) for k, v in pages.items()}


@dataclass
class Corpus:
    units: list[Unit]
    scheduled: list[Unit]
    manifest: dict[str, Any]

    def doc_text(self, doc: str) -> dict[str, str]:
        """근거 대조용: 단위 ref → 텍스트(가이드북·보도자료는 쪽마다 같은 쪽의 단위를 이어 붙임)."""
        out: dict[str, str] = {}
        for unit in self.units:
            if unit.doc != doc:
                continue
            if doc in ("GUIDE", "PRESS"):
                for page in unit.pages:
                    out[f"p{page}"] = out.get(f"p{page}", "") + "\n" + unit.text
            else:
                out[unit.ref] = unit.text
        return out


def load_corpus(directory: Path, manifest: dict[str, Any] | None = None) -> Corpus:
    manifest = manifest or load_manifest()
    units: list[Unit] = []
    scheduled: list[Unit] = []
    for doc in manifest["documents"]:
        path = directory / doc["file"]
        if not path.exists():
            raise CorpusError(f"missing file {doc['file']}")
        if doc["format"] == "hwpml_law":
            current, later = law_units(doc, hwpml_lines(path.read_text(encoding="utf-8")))
            units += current
            scheduled += later
        elif doc["format"] == "pdf_rule":
            units += rule_units(doc, pdf_pages(path.read_bytes()))
        elif doc["format"] == "pdf_guide":
            units += section_units(doc, pdf_pages(path.read_bytes()))
        else:
            raise CorpusError(f"unknown format {doc['format']}")
    return Corpus(units, scheduled, manifest)

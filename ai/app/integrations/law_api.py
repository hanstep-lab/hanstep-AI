"""법제처 국가법령정보 공동활용 API adapter (lawSearch.do / lawService.do, target=law, XML).

Field names follow the official guide checked on 2026-11-08 (lsNwListGuide, lsNwInfoGuide).
The test fixtures are hand-written from that guide; verify them against a real response once
an ``OC`` is issued (see specs/rag/api_contracts.md).
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from xml.etree.ElementTree import Element

import httpx
from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from app.rag.errors import ContractBlockedError, ValidationFailedError
from app.rag.extractors.base import Block, ExtractionReport, normalize_line

logger = logging.getLogger(__name__)
_OC_PARAM = re.compile(r"(?i)\b(OC=)[^&\s\"']+")


class _RedactOcFilter(logging.Filter):
    """httpx logs full request URLs; strip the OC credential regardless of log level."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "OC=" in message or "oc=" in message:
            record.msg = _OC_PARAM.sub(r"\1***", message)
            record.args = ()
        return True


for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).addFilter(_RedactOcFilter())

TARGET_LAWS = ("출입국관리법", "출입국관리법 시행령", "출입국관리법 시행규칙")
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_PAGES = 50
DISPLAY = 100
# 별표구분 values chunked by default. 서식 (application forms) are mostly field labels: in the
# 시행규칙 they are 286 of 299 annex units and ~1.36M characters, which would swamp retrieval.
# They stay in the raw response kept in R2 and are counted in the extraction report.
DEFAULT_ANNEX_KINDS = ("별표", "부록")
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LawApiError(Exception):
    """Non-retryable provider error (auth, schema, business error)."""


@dataclass(frozen=True)
class LawSummary:
    mst: str  # 법령일련번호
    law_id: str  # 법령ID
    name: str  # 법령명한글
    promulgation_date: str  # 공포일자 (YYYYMMDD)
    effective_date: str  # 시행일자 (YYYYMMDD)
    status_code: str  # 현행연혁코드
    kind: str  # 법령구분명
    detail_link: str


@dataclass(frozen=True)
class LawDocument:
    law_id: str
    name: str
    promulgation_date: str
    effective_date: str
    ministry: str
    raw_xml: bytes
    blocks: list[Block] = field(default_factory=list)
    article_count: int = 0
    addenda_count: int = 0
    annex_count: int = 0
    skipped_annexes: dict[str, int] = field(default_factory=dict)

    @property
    def raw_sha256(self) -> str:
        return hashlib.sha256(self.raw_xml).hexdigest()

    def content_sha256(self) -> str:
        """Hash of the parsed legal text only, ignoring volatile response fields.

        Used to decide whether a re-collection changed meaningfully. It does NOT replace
        ``source_sha256`` (which stays the hash of the stored raw response).
        """
        text = "\n".join(block.text for block in self.blocks)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text(element: Element | None, tag: str) -> str:
    if element is None:
        return ""
    found = element.find(tag)
    return normalize_line("".join(found.itertext())) if found is not None else ""


def _parse_xml(data: bytes, expected_root: str) -> Element:
    head = data.lstrip()[:200].lower()
    if head.startswith((b"<!doctype html", b"<html")):
        raise LawApiError("provider returned HTML (login/error page), not XML")
    try:
        root: Element = SafeET.fromstring(data, forbid_dtd=True)
    except (DefusedXmlException, SafeET.ParseError) as exc:
        raise LawApiError(f"invalid XML: {exc}") from exc
    if root.tag != expected_root:
        message = normalize_line("".join(root.itertext()))[:200]
        raise LawApiError(f"unexpected root <{root.tag}>: {message}")
    return root


def parse_search(data: bytes) -> tuple[int, list[LawSummary]]:
    root = _parse_xml(data, "LawSearch")
    total_text = _text(root, "totalCnt")
    if not total_text.isdigit():
        raise LawApiError("totalCnt missing in search response")
    laws = [
        LawSummary(
            mst=_text(item, "법령일련번호"),
            law_id=_text(item, "법령ID"),
            name=_text(item, "법령명한글"),
            promulgation_date=_text(item, "공포일자"),
            effective_date=_text(item, "시행일자"),
            status_code=_text(item, "현행연혁코드"),
            kind=_text(item, "법령구분명"),
            detail_link=_text(item, "법령상세링크"),
        )
        for item in root.findall("law")
    ]
    for law in laws:
        if not (law.mst and law.law_id and law.name):
            raise LawApiError("search item missing 법령일련번호/법령ID/법령명한글")
    return int(total_text), laws


def select_exact(laws: list[LawSummary], name: str) -> LawSummary:
    """Pick the single current law whose name matches exactly. Never 'first result wins'."""
    matches = [law for law in laws if law.name == name and law.status_code == "현행"]
    if len(matches) != 1:
        found = [(law.name, law.status_code, law.mst) for law in laws if law.name == name]
        raise ValidationFailedError(f"expected one current '{name}', found {found}")
    return matches[0]


def _join_items(unit: Element) -> list[str]:
    lines: list[str] = []
    content = _text(unit, "조문내용")
    if content:
        lines.append(content)
    for para in unit.findall("항"):
        para_text = _text(para, "항내용")
        if para_text:
            lines.append(para_text)
        for item in para.findall("호"):
            item_text = _text(item, "호내용")
            if item_text:
                lines.append(item_text)
            for sub in item.findall("목"):
                sub_text = _text(sub, "목내용")
                if sub_text:
                    lines.append(sub_text)
    return lines


def parse_law(data: bytes, annex_kinds: tuple[str, ...] = DEFAULT_ANNEX_KINDS) -> LawDocument:
    root = _parse_xml(data, "법령")
    info = root.find("기본정보")
    if info is None or not _text(info, "법령ID"):
        raise LawApiError("기본정보/법령ID missing in law body")
    blocks: list[Block] = []
    articles = addenda = annexes = 0
    for unit in root.iter("조문단위"):
        lines = _join_items(unit)
        if not lines:
            continue
        articles += _text(unit, "조문여부") == "조문"
        blocks.append(Block("\n".join(lines)))
    for unit in root.iter("부칙단위"):
        raw = unit.find("부칙내용")
        lines = "".join(raw.itertext()).splitlines() if raw is not None else []
        content = "\n".join(normalize_line(line) for line in lines if line.strip())
        if content:
            addenda += 1
            blocks.append(Block(content))
    skipped: dict[str, int] = {}
    for unit in root.iter("별표단위"):
        kind = _text(unit, "별표구분") or "별표"
        if kind not in annex_kinds:
            skipped[kind] = skipped.get(kind, 0) + 1
            continue
        title = _text(unit, "별표제목")
        content = _text(unit, "별표내용")
        if title or content:
            annexes += 1
            blocks.append(Block("\n".join(part for part in (title, content) if part)))
    return LawDocument(
        law_id=_text(info, "법령ID"),
        name=_text(info, "법령명_한글"),
        promulgation_date=_text(info, "공포일자"),
        effective_date=_text(info, "시행일자"),
        ministry=_text(info, "소관부처"),
        raw_xml=data,
        blocks=blocks,
        article_count=articles,
        addenda_count=addenda,
        annex_count=annexes,
        skipped_annexes=skipped,
    )


def law_extraction_report(doc: LawDocument) -> ExtractionReport:
    report = ExtractionReport("ok", "law-api-xml", blocks=list(doc.blocks))
    report.paragraphs = len(doc.blocks)
    report.characters = sum(len(b.text) for b in doc.blocks)
    notes = []
    if doc.addenda_count == 0:
        notes.append("no 부칙 parsed")
    if doc.skipped_annexes:
        skipped = ", ".join(f"{kind} {n}" for kind, n in sorted(doc.skipped_annexes.items()))
        notes.append(f"annexes not chunked (kept in raw response): {skipped}")
    report.reason = "; ".join(notes)
    if not doc.blocks:
        report.status, report.reason = "failed", "law body has no text"
    return report


class LawApiClient:
    def __init__(
        self,
        *,
        base_url: str,
        oc: str,
        client: httpx.Client | None = None,
        max_attempts: int = 4,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not oc:
            raise ContractBlockedError("LAW_API_OC is not set; request an OC from open.law.go.kr")
        if not base_url.startswith("https://"):
            raise ValueError("law API base URL must be https")
        self._oc = oc
        self._client = client or httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
            follow_redirects=False,
        )
        self._max_attempts = max_attempts
        self._sleep = sleep

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict[str, str | int]) -> bytes:
        query = {"OC": self._oc, "type": "XML", **params}
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._client.get(path, params=query)
            except httpx.TransportError as exc:
                if attempt == self._max_attempts:
                    raise LawApiError(f"network error after {attempt} attempts") from exc
                self._sleep(min(2**attempt, 30))
                continue
            if response.is_redirect:
                # Never forward the OC to another host.
                raise LawApiError(f"unexpected redirect ({response.status_code})")
            if response.status_code in RETRYABLE_STATUS and attempt < self._max_attempts:
                retry_after = response.headers.get("Retry-After", "")
                delay = float(retry_after) if retry_after.isdigit() else min(2**attempt, 30)
                logger.warning("law API %s returned %d, retrying", path, response.status_code)
                self._sleep(delay)
                continue
            if response.status_code != 200:
                raise LawApiError(f"{path} returned HTTP {response.status_code}")
            if len(response.content) > MAX_RESPONSE_BYTES:
                raise LawApiError("response too large")
            return response.content
        raise LawApiError("retry budget exhausted")  # pragma: no cover

    def search_all(self, query: str) -> list[LawSummary]:
        """All pages of a law-name search, detecting repeated pages and duplicates."""
        results: dict[str, LawSummary] = {}
        seen_pages: set[tuple[str, ...]] = set()
        total = None
        for page in range(1, MAX_PAGES + 1):
            data = self._get(
                "/DRF/lawSearch.do",
                {"target": "law", "search": 1, "query": query, "display": DISPLAY, "page": page},
            )
            total, laws = parse_search(data)
            if not laws:
                break
            signature = tuple(law.mst for law in laws)
            if signature in seen_pages:
                raise LawApiError(f"page {page} repeats an earlier page")
            seen_pages.add(signature)
            for law in laws:
                results.setdefault(law.mst, law)
            if len(results) >= total:
                break
        else:
            raise LawApiError("search exceeded MAX_PAGES")
        if total is not None and len(results) < total:
            raise LawApiError(f"collected {len(results)} of {total} search results")
        return list(results.values())

    def fetch_law(self, mst: str) -> LawDocument:
        if not mst.isdigit():
            raise ValueError("MST must be numeric")
        return parse_law(self._get("/DRF/lawService.do", {"target": "law", "MST": mst}))

    def collect_targets(self, names: tuple[str, ...] = TARGET_LAWS) -> Iterator[LawDocument]:
        for name in names:
            summary = select_exact(self.search_all(name), name)
            doc = self.fetch_law(summary.mst)
            if doc.law_id != summary.law_id:
                raise LawApiError(f"body 법령ID {doc.law_id} != search {summary.law_id}")
            yield doc

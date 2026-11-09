"""HWPX (OWPML ZIP/XML) extraction with ZIP bomb, path traversal and XXE protection."""

from __future__ import annotations

import io
import re
import zipfile
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from app.rag.extractors.base import Block, ExtractionReport, garbled_ratio, normalize_line

HP = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
MAX_MEMBERS = 500
MAX_TOTAL_UNCOMPRESSED = 100 * 1024 * 1024
MAX_MEMBER_UNCOMPRESSED = 30 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
_SECTION = re.compile(r"^Contents/section(\d+)\.xml$")


def _check_archive(archive: zipfile.ZipFile) -> str | None:
    infos = archive.infolist()
    if len(infos) > MAX_MEMBERS:
        return f"too many ZIP members ({len(infos)})"
    total = 0
    for info in infos:
        name = info.filename
        if name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/"):
            return f"unsafe ZIP member path: {name}"
        if info.file_size > MAX_MEMBER_UNCOMPRESSED:
            return f"ZIP member too large: {name}"
        if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
            return f"suspicious compression ratio: {name}"
        total += info.file_size
    if total > MAX_TOTAL_UNCOMPRESSED:
        return "ZIP uncompressed size exceeds limit"
    return None


def _read_limited(archive: zipfile.ZipFile, name: str) -> bytes:
    # Declared sizes can lie; cap the bytes actually inflated.
    with archive.open(name) as handle:
        data = handle.read(MAX_MEMBER_UNCOMPRESSED + 1)
    if len(data) > MAX_MEMBER_UNCOMPRESSED:
        raise ValueError(f"ZIP member inflates beyond limit: {name}")
    return data


def _paragraph_text(paragraph: Element) -> str:
    """Text of a paragraph's own runs, excluding nested table cells."""
    parts: list[str] = []
    for run in paragraph.findall(f"{HP}run"):
        for child in run:
            if child.tag == f"{HP}t":
                parts.append("".join(child.itertext()))
            elif child.tag == f"{HP}tab":
                parts.append(" ")
    return normalize_line("".join(parts))


def _cell_text(cell: Element) -> str:
    texts = [_paragraph_text(p) for p in cell.iter(f"{HP}p")]
    return " ".join(t for t in texts if t)


def _table_rows(table: Element) -> list[str]:
    rows: list[str] = []
    for row in table.findall(f"{HP}tr"):
        cells = [_cell_text(cell) for cell in row.findall(f"{HP}tc")]
        if any(cells):
            rows.append(" | ".join(cells))
    return rows


def _walk(container: Element, blocks: list[Block]) -> None:
    for paragraph in container.findall(f"{HP}p"):
        text = _paragraph_text(paragraph)
        if text:
            blocks.append(Block(text))
        for run in paragraph.findall(f"{HP}run"):
            for table in run.findall(f"{HP}tbl"):
                rows = _table_rows(table)
                if rows:
                    header = rows[0] if len(rows) > 1 else None
                    blocks.append(Block("\n".join(rows), kind="table", table_header=header))


def extract_hwpx(data: bytes) -> ExtractionReport:
    report = ExtractionReport("ok", "hwpx-owpml")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            problem = _check_archive(archive)
            if problem:
                report.status, report.reason = "failed", problem
                return report
            sections = sorted(
                (int(m.group(1)), name)
                for name in archive.namelist()
                if (m := _SECTION.match(name))
            )
            if not sections:
                report.status, report.reason = "failed", "no Contents/section*.xml in HWPX"
                return report
            for _, name in sections:
                root = SafeET.fromstring(_read_limited(archive, name), forbid_dtd=True)
                _walk(root, report.blocks)
    except (zipfile.BadZipFile, ValueError, DefusedXmlException, SafeET.ParseError) as exc:
        report.status, report.reason = "failed", f"unreadable HWPX: {exc}"
        report.blocks = []
        return report

    joined = "\n".join(block.text for block in report.blocks)
    report.paragraphs = sum(1 for b in report.blocks if b.kind == "paragraph")
    report.tables = sum(1 for b in report.blocks if b.kind == "table")
    report.characters = len(joined)
    report.garbled_ratio = garbled_ratio(joined)
    if not report.blocks:
        report.status, report.reason = "failed", "HWPX contains no text"
    # HWPX stores no rendered page layout, so page_number stays unknown.
    report.page_numbers_reliable = False
    return report

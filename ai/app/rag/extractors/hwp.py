"""HWP 5.x binary extraction (OLE compound file + zlib-compressed record streams).

Implements the public Hancom "한글 문서 파일 형식 5.0" layout: FileHeader flags, BodyText/
SectionN record trees, PARA_TEXT, and table controls (TABLE + per-cell LIST_HEADER) so table
rows/columns survive. Encrypted and distribution (배포용) documents are refused. HWP has no
rendered page layout, so page numbers are never produced.
"""

from __future__ import annotations

import io
import re
import struct
import zlib
from dataclasses import dataclass, field

import olefile

from app.rag.extractors.base import Block, ExtractionReport, garbled_ratio, normalize_line

SIGNATURE = b"HWP Document File"
MAX_SECTIONS = 200
MAX_SECTION_BYTES = 50 * 1024 * 1024
MAX_RECORDS = 2_000_000
MAX_TABLE_CELLS = 10_000

TAG_PARA_HEADER = 66
TAG_PARA_TEXT = 67
TAG_CTRL_HEADER = 71
TAG_LIST_HEADER = 72
TAG_TABLE = 77

# Controls whose nested paragraphs are page decoration, not document content.
SKIPPED_CONTROLS = {b"head", b"foot"}
# WCHAR control codes that occupy 8 code units (inline and extended controls).
_WIDE_CONTROLS = {1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23}
_SECTION = re.compile(r"^Section(\d+)$")


class HwpFormatError(ValueError):
    pass


@dataclass
class Record:
    tag: int
    level: int
    data: bytes
    children: list[Record] = field(default_factory=list)


def parse_records(data: bytes) -> list[Record]:
    """Parse a record stream into a tree using each record's level."""
    roots: list[Record] = []
    stack: list[Record] = []
    offset = 0
    count = 0
    while offset < len(data):
        if offset + 4 > len(data):
            raise HwpFormatError("truncated record header")
        header = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        tag, level, size = header & 0x3FF, (header >> 10) & 0x3FF, (header >> 20) & 0xFFF
        if size == 0xFFF:
            if offset + 4 > len(data):
                raise HwpFormatError("truncated extended record size")
            size = struct.unpack_from("<I", data, offset)[0]
            offset += 4
        if offset + size > len(data):
            raise HwpFormatError("record exceeds stream length")
        record = Record(tag, level, data[offset : offset + size])
        offset += size
        count += 1
        if count > MAX_RECORDS:
            raise HwpFormatError("too many records")
        while stack and stack[-1].level >= level:
            stack.pop()
        if level == 0 or not stack:
            if level != 0 and not stack:
                raise HwpFormatError("record level without parent")
            roots.append(record)
        elif level == stack[-1].level + 1:
            stack[-1].children.append(record)
        else:
            raise HwpFormatError("record level skips a generation")
        stack.append(record)
    return roots


def decode_para_text(data: bytes) -> str:
    """Decode PARA_TEXT WCHARs, dropping inline/extended control payloads."""
    out = bytearray()
    index = 0
    while index + 2 <= len(data):
        code = struct.unpack_from("<H", data, index)[0]
        if code in _WIDE_CONTROLS:
            if code == 9:  # tab
                out += b" \x00"
            index += 16
            continue
        if code in (10, 13):
            out += b"\n\x00"
        elif code == 24:  # hyphen
            out += b"-\x00"
        elif code in (30, 31):  # non-breaking / fixed-width space
            out += b" \x00"
        elif code >= 32:
            out += data[index : index + 2]
        index += 2
    return out.decode("utf-16-le", errors="replace")


def _ctrl_id(record: Record) -> bytes:
    return record.data[:4][::-1] if len(record.data) >= 4 else b""


@dataclass
class _Walker:
    blocks: list[Block] = field(default_factory=list)
    tables: int = 0
    skipped_controls: int = 0

    def paragraph_text(self, para: Record) -> str:
        texts = [decode_para_text(c.data) for c in para.children if c.tag == TAG_PARA_TEXT]
        lines = (normalize_line(line) for line in "".join(texts).split("\n"))
        return "\n".join(line for line in lines if line)

    def walk_paragraphs(self, records: list[Record]) -> None:
        for para in records:
            if para.tag != TAG_PARA_HEADER:
                continue
            text = self.paragraph_text(para)
            if text:
                self.blocks.append(Block(text))
            for ctrl in para.children:
                if ctrl.tag == TAG_CTRL_HEADER:
                    self.walk_control(ctrl)

    def walk_control(self, ctrl: Record) -> None:
        ctrl_id = _ctrl_id(ctrl)
        if ctrl_id in SKIPPED_CONTROLS:
            self.skipped_controls += 1
            return
        if ctrl_id == b"tbl ":
            self.walk_table(ctrl)
            return
        # Footnotes, endnotes, text boxes: keep their paragraphs in reading order.
        self.walk_paragraphs(ctrl.children)

    def cell_text(self, paragraphs: list[Record]) -> str:
        """Cell content flattened to one line; nested tables become inline rows."""
        nested = _Walker()
        nested.walk_paragraphs(paragraphs)
        self.tables += nested.tables
        return " ".join(block.text.replace("\n", " ") for block in nested.blocks)

    def walk_table(self, ctrl: Record) -> None:
        table = next((c for c in ctrl.children if c.tag == TAG_TABLE), None)
        if table is None or len(table.data) < 8:
            raise HwpFormatError("table control without TABLE record")
        rows, cols = struct.unpack_from("<HH", table.data, 4)
        if rows * cols > MAX_TABLE_CELLS:
            raise HwpFormatError("table too large")
        seen_table = False
        caption: list[Record] = []
        cells: list[tuple[int, int, list[Record]]] = []
        current: list[Record] | None = None
        for child in ctrl.children:
            if child.tag == TAG_TABLE:
                seen_table = True
            elif child.tag == TAG_LIST_HEADER:
                if not seen_table:
                    current = caption  # caption list precedes the TABLE record
                    continue
                if len(child.data) < 16:
                    raise HwpFormatError("cell LIST_HEADER too short")
                col, row = struct.unpack_from("<HH", child.data, 8)
                current = []
                cells.append((row, col, current))
            elif child.tag == TAG_PARA_HEADER and current is not None:
                current.append(child)

        caption_text = self.cell_text(caption)
        if caption_text:
            self.blocks.append(Block(caption_text))
        grid = [["" for _ in range(cols)] for _ in range(rows)]
        for row, col, paragraphs in cells:
            if row >= rows or col >= cols:
                raise HwpFormatError(f"cell address ({row},{col}) outside {rows}x{cols} table")
            grid[row][col] = self.cell_text(paragraphs)
        lines = [" | ".join(r) for r in grid if any(r)]
        self.tables += 1
        if lines:
            header = lines[0] if len(lines) > 1 else None
            self.blocks.append(Block("\n".join(lines), kind="table", table_header=header))


def _read_stream(ole: olefile.OleFileIO, path: str, compressed: bool) -> bytes:
    raw = ole.openstream(path).read(MAX_SECTION_BYTES + 1)
    if len(raw) > MAX_SECTION_BYTES:
        raise HwpFormatError(f"{path} too large")
    if not compressed:
        return bytes(raw)
    inflater = zlib.decompressobj(-15)
    data = inflater.decompress(raw, MAX_SECTION_BYTES + 1)
    if len(data) > MAX_SECTION_BYTES or inflater.unconsumed_tail:
        raise HwpFormatError(f"{path} inflates beyond limit")
    return data


def extract_hwp(data: bytes) -> ExtractionReport:
    report = ExtractionReport("ok", "hwp5-records")
    try:
        if not olefile.isOleFile(io.BytesIO(data)):
            raise HwpFormatError("not an OLE compound file")
        with olefile.OleFileIO(io.BytesIO(data)) as ole:
            if not ole.exists("FileHeader"):
                raise HwpFormatError("FileHeader stream missing")
            header = ole.openstream("FileHeader").read(256)
            if not header.startswith(SIGNATURE) or len(header) < 40:
                raise HwpFormatError("not an HWP 5.x document")
            major = header[35]
            if major != 5:
                raise HwpFormatError(f"unsupported HWP major version {major}")
            flags = struct.unpack_from("<I", header, 36)[0]
            if flags & 0x2:
                report.status, report.reason = "failed", "password-protected HWP"
                report.remedy = "Obtain an unprotected original from the publisher."
                return report
            if flags & 0x4:
                report.status, report.reason = "failed", "distribution (배포용) HWP"
                report.remedy = "Distribution documents are encrypted; request the original."
                return report
            compressed = bool(flags & 0x1)
            sections = sorted(
                (int(m.group(1)), "/".join(entry))
                for entry in ole.listdir()
                if len(entry) == 2 and entry[0] == "BodyText" and (m := _SECTION.match(entry[1]))
            )
            if not sections:
                raise HwpFormatError("no BodyText sections")
            if len(sections) > MAX_SECTIONS:
                raise HwpFormatError("too many sections")
            walker = _Walker()
            for _, path in sections:
                walker.walk_paragraphs(parse_records(_read_stream(ole, path, compressed)))
    except (HwpFormatError, OSError, zlib.error, struct.error) as exc:
        report.status, report.reason = "failed", f"unreadable HWP: {exc}"
        report.blocks = []
        return report

    report.blocks = walker.blocks
    joined = "\n".join(block.text for block in report.blocks)
    report.paragraphs = sum(1 for b in report.blocks if b.kind == "paragraph")
    report.tables = walker.tables
    report.characters = len(joined)
    report.garbled_ratio = garbled_ratio(joined)
    report.page_numbers_reliable = False
    if not report.blocks:
        report.status, report.reason = "failed", "HWP contains no text"
    elif report.garbled_ratio > 0.05:
        report.status = "failed"
        report.reason = f"garbled character ratio {report.garbled_ratio:.2%}"
        report.blocks = []
    if report.status == "ok" and walker.skipped_controls:
        report.reason = f"skipped {walker.skipped_controls} header/footer controls"
    return report

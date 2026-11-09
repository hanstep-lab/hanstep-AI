"""Extraction result types and text normalization shared by all extractors."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

ExtractionStatus = Literal["ok", "needs_ocr", "needs_conversion", "failed"]
BlockKind = Literal["paragraph", "table"]

# Page footers such as "- 1 -" carry no content.
_PAGE_FOOTER = re.compile(r"^\s*-\s*\d+\s*-\s*$")
_INLINE_SPACE = re.compile(r"[ \t 　]+")
# Replacement char or private-use glyphs signal broken font mappings.
_GARBLED = re.compile(r"[�-]")


def normalize_line(line: str) -> str:
    """NFC + whitespace collapse only.

    NFKC is deliberately avoided: it rewrites circled numbers, units (㎡) and full-width
    symbols that carry meaning in policy text.
    """
    return _INLINE_SPACE.sub(" ", unicodedata.normalize("NFC", line)).strip()


def normalize_text(text: str) -> tuple[str, int]:
    """Normalize a page/paragraph. Returns the text and the number of dropped footer lines."""
    lines: list[str] = []
    dropped = 0
    for raw in text.splitlines():
        line = normalize_line(raw)
        if _PAGE_FOOTER.match(line):
            dropped += 1
            continue
        lines.append(line)
    # Collapse runs of blank lines to one paragraph break.
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return out, dropped


def garbled_ratio(text: str) -> float:
    visible = [c for c in text if not c.isspace()]
    if not visible:
        return 0.0
    return len(_GARBLED.findall(text)) / len(visible)


@dataclass(frozen=True)
class Block:
    """A contiguous unit of source text in reading order."""

    text: str
    kind: BlockKind = "paragraph"
    page_number: int | None = None
    # For tables: the header row, repeated when a long table is split.
    table_header: str | None = None


@dataclass
class ExtractionReport:
    status: ExtractionStatus
    extractor: str
    blocks: list[Block] = field(default_factory=list)
    pages: int | None = None
    empty_pages: list[int] = field(default_factory=list)
    paragraphs: int = 0
    tables: int = 0
    characters: int = 0
    garbled_ratio: float = 0.0
    dropped_footer_lines: int = 0
    reason: str = ""
    remedy: str = ""
    # When text came from a derivative (conversion/OCR), its hash; the official hash stays
    # the original file's.
    derived_sha256: str | None = None
    page_numbers_reliable: bool = False

    def summary(self) -> dict[str, object]:
        return {
            "status": self.status,
            "extractor": self.extractor,
            "pages": self.pages,
            "empty_pages": self.empty_pages,
            "paragraphs": self.paragraphs,
            "tables": self.tables,
            "characters": self.characters,
            "garbled_ratio": round(self.garbled_ratio, 4),
            "dropped_footer_lines": self.dropped_footer_lines,
            "reason": self.reason,
            "remedy": self.remedy,
            "derived_sha256": self.derived_sha256,
            "page_numbers_reliable": self.page_numbers_reliable,
        }

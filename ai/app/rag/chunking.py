"""Deterministic structure-aware chunking of Korean policy text.

Chunks never cross a page boundary when page numbers are reliable, so each chunk maps to a
single ``page_number``. Oversized input is split, never silently truncated.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field

from app.rag.errors import RagError
from app.rag.extractors.base import Block

CHUNKER_VERSION = "1"

TokenCounter = Callable[[str], int]

# Major structural headings: 제N장/절/조, roman numerals, "1 선발인원", "1. 개요", □, <제목>.
_HEADING = re.compile(
    r"^(제\s*\d+\s*[장절관]|제\s*\d+\s*조(?:\s*의\s*\d+)?|[IVXⅠ-Ⅹ]+[.\s]|\d{1,2}\.?\s+[가-힣]"
    r"|□|■|<\s*[^<>|]{2,40}>)"
)
_SENTENCE_END = re.compile(r"(?<=[.?!다요])\s+")


class ChunkingError(RagError):
    """A chunk could not be kept within max_tokens."""


def approx_token_count(text: str) -> int:
    """Conservative offline estimate used only when the real tokenizer is unavailable."""
    return math.ceil(len(text) / 1.6)


@dataclass(frozen=True)
class ChunkerConfig:
    target_tokens: int = 600
    max_tokens: int = 900
    overlap_tokens: int = 80
    # Flush at a heading once the current chunk has at least this share of the target.
    heading_flush_ratio: float = 0.4

    def __post_init__(self) -> None:
        if not 0 < self.target_tokens <= self.max_tokens:
            raise ValueError("require 0 < target_tokens <= max_tokens")
        if not 0 <= self.overlap_tokens < self.target_tokens:
            raise ValueError("require 0 <= overlap_tokens < target_tokens")


@dataclass(frozen=True)
class ChunkDraft:
    chunk_index: int
    text: str
    token_count: int
    page_number: int | None
    section_title: str | None
    # Processing metadata kept in the run report, not in MongoDB.
    kinds: tuple[str, ...] = ()
    repeated_table_header: bool = False
    overlap_lines: int = 0


def is_heading(line: str) -> bool:
    return len(line) <= 50 and " | " not in line and bool(_HEADING.match(line))


def fingerprint(config: ChunkerConfig, extractor: str, tokenizer_id: str) -> str:
    payload = {
        "chunker_version": CHUNKER_VERSION,
        "config": asdict(config),
        "extractor": extractor,
        "tokenizer": tokenizer_id,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass
class _Line:
    text: str
    page: int | None
    kind: str
    heading: bool = False
    table_header: str | None = None


@dataclass
class _Builder:
    config: ChunkerConfig
    count: TokenCounter
    drafts: list[ChunkDraft] = field(default_factory=list)
    lines: list[_Line] = field(default_factory=list)
    section: str | None = None
    chunk_section: str | None = None
    repeated_header: bool = False
    overlap_lines: int = 0

    def tokens(self, extra: str | None = None) -> int:
        texts = [line.text for line in self.lines]
        if extra is not None:
            texts.append(extra)
        return self.count("\n".join(texts)) if texts else 0

    def flush(self, *, carry_overlap: bool) -> None:
        if not self.lines:
            return
        text = "\n".join(line.text for line in self.lines)
        tokens = self.count(text)
        if tokens > self.config.max_tokens:
            raise ChunkingError(
                f"chunk of {tokens} tokens exceeds max_tokens={self.config.max_tokens}"
            )
        self.drafts.append(
            ChunkDraft(
                chunk_index=len(self.drafts),
                text=text,
                token_count=tokens,
                page_number=self.lines[0].page,
                section_title=self.chunk_section,
                kinds=tuple(sorted({line.kind for line in self.lines})),
                repeated_table_header=self.repeated_header,
                overlap_lines=self.overlap_lines,
            )
        )
        tail: list[_Line] = []
        if carry_overlap and self.config.overlap_tokens:
            for line in reversed(self.lines):
                candidate = [line, *tail]
                if self.count("\n".join(item.text for item in candidate)) > (
                    self.config.overlap_tokens
                ):
                    break
                tail = candidate
            # Never let the overlap be the whole chunk.
            if len(tail) == len(self.lines):
                tail = tail[1:]
        self.lines = tail
        self.overlap_lines = len(tail)
        self.repeated_header = False
        self.chunk_section = self.section

    def add(self, line: _Line) -> None:
        if self.lines and self.lines[-1].page != line.page:
            self.flush(carry_overlap=False)
        if line.heading:
            if self.lines and self.tokens() >= (
                self.config.target_tokens * self.config.heading_flush_ratio
            ):
                self.flush(carry_overlap=False)
            self.section = line.text
            if not self.lines or self.overlap_lines == len(self.lines):
                self.lines = []
                self.overlap_lines = 0
                self.chunk_section = line.text
        only_repeated_header = self.repeated_header and len(self.lines) == 1
        if (
            self.lines
            and not only_repeated_header
            and self.tokens(line.text) > self.config.target_tokens
        ):
            self.flush(carry_overlap=line.kind != "table")
            if self.tokens(line.text) > self.config.max_tokens:
                self.lines = []
                self.overlap_lines = 0
        if not self.lines:
            self.chunk_section = self.section
        self.lines.append(line)


def _split_long(text: str, count: TokenCounter, limit: int) -> list[str]:
    """Split one over-long line on sentence ends, then on character windows."""
    if count(text) <= limit:
        return [text]
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(text):
        candidate = f"{current} {sentence}".strip()
        if count(candidate) <= limit:
            current = candidate
            continue
        if current:
            pieces.append(current)
        if count(sentence) <= limit:
            current = sentence
            continue
        # Hard split: binary search the longest prefix that fits.
        rest = sentence
        while count(rest) > limit:
            lo, hi = 1, len(rest)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if count(rest[:mid]) <= limit:
                    lo = mid
                else:
                    hi = mid - 1
            pieces.append(rest[:lo])
            rest = rest[lo:]
        current = rest
    if current:
        pieces.append(current)
    return pieces


def _expand(blocks: Iterable[Block], count: TokenCounter, config: ChunkerConfig) -> list[_Line]:
    lines: list[_Line] = []
    # Leave room so a single line plus a repeated table header always fits in max_tokens.
    line_limit = config.max_tokens // 2
    for block in blocks:
        if block.kind == "table":
            rows = block.text.split("\n")
            for index, row in enumerate(rows):
                for piece in _split_long(row, count, line_limit):
                    lines.append(
                        _Line(
                            piece,
                            block.page_number,
                            "table",
                            table_header=block.table_header if index > 0 else None,
                        )
                    )
            continue
        for raw in block.text.split("\n"):
            if not raw.strip():
                continue
            heading = is_heading(raw)
            for piece in _split_long(raw, count, line_limit):
                lines.append(_Line(piece, block.page_number, "paragraph", heading=heading))
                heading = False
    return lines


def chunk_blocks(
    blocks: Iterable[Block], count: TokenCounter, config: ChunkerConfig | None = None
) -> list[ChunkDraft]:
    config = config or ChunkerConfig()
    builder = _Builder(config, count)
    previous_header: str | None = None
    for line in _expand(blocks, count, config):
        # A table row continuing into a new chunk gets its header repeated (marked as copy).
        if line.kind == "table" and line.table_header:
            would_flush = bool(builder.lines) and (
                builder.tokens(line.text) > config.target_tokens
                or builder.lines[-1].page != line.page
            )
            if would_flush:
                builder.flush(carry_overlap=False)
            header_fits = count(f"{line.table_header}\n{line.text}") <= config.max_tokens
            if not builder.lines and previous_header == line.table_header and header_fits:
                builder.lines.append(_Line(line.table_header, line.page, "table"))
                builder.repeated_header = True
                builder.chunk_section = builder.section
            builder.add(line)
            previous_header = line.table_header
            continue
        previous_header = None if line.kind != "table" else line.text
        builder.add(line)
    builder.flush(carry_overlap=False)
    return builder.drafts

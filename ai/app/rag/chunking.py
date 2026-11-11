"""한국어 정책 문서의 결정적·구조 인식 청킹.

페이지 번호를 신뢰할 수 있으면 청크가 페이지 경계를 넘지 않으므로, 각 청크는 하나의
``page_number``에 대응한다. 너무 긴 입력은 나누며, 조용히 잘라내지 않는다.
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

# 주요 구조 제목: 제N장/절/조, 로마 숫자, "1 선발인원", "1. 개요", □, <제목>.
_HEADING = re.compile(
    r"^(제\s*\d+\s*[장절관]|제\s*\d+\s*조(?:\s*의\s*\d+)?|[IVXⅠ-Ⅹ]+[.\s]|\d{1,2}\.?\s+[가-힣]"
    r"|□|■|<\s*[^<>|]{2,40}>)"
)
_SENTENCE_END = re.compile(r"(?<=[.?!다요])\s+")


class ChunkingError(RagError):
    """청크를 max_tokens 이내로 유지할 수 없다."""


def approx_token_count(text: str) -> int:
    """실제 토크나이저를 쓸 수 없을 때만 사용하는 보수적인 오프라인 추정치."""
    return math.ceil(len(text) / 1.6)


@dataclass(frozen=True)
class ChunkerConfig:
    """청크 목표·최대·겹침 토큰 수와 제목 분할 비율 설정."""

    target_tokens: int = 600
    max_tokens: int = 900
    overlap_tokens: int = 80
    # 현재 청크가 목표의 이 비율 이상 찼으면 제목에서 청크를 끊는다.
    heading_flush_ratio: float = 0.4

    def __post_init__(self) -> None:
        if not 0 < self.target_tokens <= self.max_tokens:
            raise ValueError("require 0 < target_tokens <= max_tokens")
        if not 0 <= self.overlap_tokens < self.target_tokens:
            raise ValueError("require 0 <= overlap_tokens < target_tokens")


@dataclass(frozen=True)
class ChunkDraft:
    """임베딩·저장 전에 청커가 만든 청크 초안."""

    chunk_index: int
    text: str
    token_count: int
    page_number: int | None
    section_title: str | None
    # MongoDB가 아니라 실행 보고서에만 남기는 처리 메타데이터.
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
    """블록을 줄 단위로 펼친 청킹 입력 한 줄."""

    text: str
    page: int | None
    kind: str
    heading: bool = False
    table_header: str | None = None


@dataclass
class _Builder:
    """줄을 누적해 ChunkDraft 목록을 만드는 청킹 상태."""

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
            # 겹침이 청크 전체가 되지 않게 한다.
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
    """너무 긴 한 줄을 먼저 문장 끝에서, 그다음 문자 구간 단위로 나눈다."""
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
        # 강제 분할: 한도에 맞는 가장 긴 접두부를 이진 탐색한다.
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
    # 한 줄과 반복된 표 헤더를 합쳐도 항상 max_tokens에 들어가도록 여유를 둔다.
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
        # 새 청크로 이어지는 표 행에는 헤더를 반복해 붙인다(사본으로 표시).
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

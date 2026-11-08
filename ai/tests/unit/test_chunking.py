from __future__ import annotations

import pytest

from app.rag.chunking import (
    ChunkerConfig,
    approx_token_count,
    chunk_blocks,
    fingerprint,
    is_heading,
)
from app.rag.extractors import extract
from app.rag.extractors.base import Block
from tests.conftest import DATA_ROOT

pytestmark = pytest.mark.unit

SMALL = ChunkerConfig(target_tokens=60, max_tokens=90, overlap_tokens=10)


def _lines(n: int, prefix: str = "조건") -> str:
    return "\n".join(f"- {prefix} {i}: 연봉 2,600만원 이상 근무 요건을 충족할 것" for i in range(n))


def test_chunks_are_deterministic_and_within_limits() -> None:
    blocks = [Block(_lines(30), page_number=1), Block(_lines(30, "예외"), page_number=2)]
    first = chunk_blocks(blocks, approx_token_count, SMALL)
    second = chunk_blocks(blocks, approx_token_count, SMALL)
    assert first == second
    assert [c.chunk_index for c in first] == list(range(len(first)))
    assert all(c.token_count <= SMALL.max_tokens for c in first)
    assert len(first) > 2


def test_chunks_never_cross_pages() -> None:
    blocks = [Block("1페이지 문장", page_number=1), Block("2페이지 문장", page_number=2)]
    chunks = chunk_blocks(blocks, approx_token_count, SMALL)
    assert [(c.page_number, c.text) for c in chunks] == [(1, "1페이지 문장"), (2, "2페이지 문장")]


def test_overlap_stays_within_page_and_is_bounded() -> None:
    config = ChunkerConfig(target_tokens=60, max_tokens=90, overlap_tokens=30)
    chunks = chunk_blocks([Block(_lines(30), page_number=1)], approx_token_count, config)
    assert all(c.token_count <= config.max_tokens for c in chunks)
    overlapped = [c for c in chunks if c.overlap_lines]
    assert overlapped
    for previous, current in zip(chunks, chunks[1:], strict=False):
        if current.overlap_lines:
            tail = previous.text.split("\n")[-current.overlap_lines :]
            assert current.text.split("\n")[: current.overlap_lines] == tail


def test_overlong_line_is_split_not_truncated() -> None:
    sentence = "외국인 근로자는 요건을 충족해야 한다. " * 80
    chunks = chunk_blocks([Block(sentence.strip(), page_number=1)], approx_token_count, SMALL)
    assert len(chunks) > 1
    assert all(c.token_count <= SMALL.max_tokens for c in chunks)
    rebuilt = " ".join(c.text for c in chunks).replace(" ", "")
    assert rebuilt == sentence.replace(" ", "")


def test_headings_start_sections() -> None:
    text = "1 선발인원 : 33,000명\n" + _lines(3) + "\n2 신청 대상\n" + _lines(3, "대상")
    chunks = chunk_blocks([Block(text, page_number=1)], approx_token_count, SMALL)
    titles = {c.section_title for c in chunks}
    assert "1 선발인원 : 33,000명" in titles
    assert "2 신청 대상" in titles
    assert is_heading("제12조(정의)")
    assert not is_heading("경기 | 2,145 | 1,215")


def test_long_table_repeats_header_and_marks_it() -> None:
    header = "구분 | 인원 | 비고"
    rows = [header] + [f"지역{i} | {i * 100:,} | 광역지자체 추천 배정" for i in range(40)]
    block = Block("\n".join(rows), kind="table", page_number=None, table_header=header)
    chunks = chunk_blocks([block], approx_token_count, SMALL)
    assert len(chunks) > 1
    assert chunks[0].repeated_table_header is False
    for chunk in chunks[1:]:
        assert chunk.text.split("\n")[0] == header
        assert chunk.repeated_table_header is True
        assert chunk.token_count <= SMALL.max_tokens


def test_real_documents_chunk_within_default_limits() -> None:
    for path in (
        "visa/selection/e74_selection_20260601.pdf",
        "visa/recommendations/local/gyeonggi/e74_gyeonggi_20260601.hwpx",
    ):
        data = (DATA_ROOT / path).read_bytes()
        report = extract("pdf" if path.endswith(".pdf") else "hwpx", data)
        chunks = chunk_blocks(report.blocks, approx_token_count)
        assert chunks
        assert all(c.token_count <= 900 for c in chunks)
        assert all(c.text.strip() for c in chunks)


def test_fingerprint_changes_with_settings() -> None:
    a = fingerprint(ChunkerConfig(), "pypdf", "tok")
    assert a == fingerprint(ChunkerConfig(), "pypdf", "tok")
    assert a != fingerprint(ChunkerConfig(target_tokens=500), "pypdf", "tok")
    assert a != fingerprint(ChunkerConfig(), "pypdf", "other-tokenizer")


def test_invalid_config_rejected() -> None:
    with pytest.raises(ValueError):
        ChunkerConfig(target_tokens=1000, max_tokens=900)
    with pytest.raises(ValueError):
        ChunkerConfig(target_tokens=100, overlap_tokens=100)

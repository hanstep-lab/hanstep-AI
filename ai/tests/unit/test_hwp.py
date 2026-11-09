from __future__ import annotations

import struct

import pytest

from app.rag.extractors.hwp import (
    HwpFormatError,
    decode_para_text,
    extract_hwp,
    parse_records,
)
from tests.conftest import DATA_ROOT

pytestmark = pytest.mark.unit

SAMPLE = DATA_ROOT / "visa/guides/e74_central_agency_guide_2026.hwp"


def _record(tag: int, level: int, payload: bytes) -> bytes:
    return struct.pack("<I", tag | (level << 10) | (len(payload) << 20)) + payload


def _with_flags(flags: int) -> bytes:
    data = bytearray(SAMPLE.read_bytes())
    offset = data.find(b"HWP Document File")
    assert offset >= 0
    struct.pack_into("<I", data, offset + 36, flags)
    return bytes(data)


def test_all_real_hwp_files_extract() -> None:
    for path in sorted(DATA_ROOT.rglob("*.hwp")):
        report = extract_hwp(path.read_bytes())
        assert report.status == "ok", (path.name, report.reason)
        assert report.characters > 1000
        assert report.garbled_ratio < 0.01


@pytest.mark.parametrize(("flags", "reason"), [(0x3, "password"), (0x5, "배포용")])
def test_protected_documents_refused(flags: int, reason: str) -> None:
    report = extract_hwp(_with_flags(flags))
    assert report.status == "failed"
    assert reason in report.reason
    assert report.blocks == []


def test_uncompressed_flag_on_compressed_stream_fails_cleanly() -> None:
    report = extract_hwp(_with_flags(0x0))
    assert report.status == "failed"


def test_non_ole_input_fails() -> None:
    assert extract_hwp(b"%PDF-1.7 not hwp").status == "failed"


def test_decode_para_text_skips_control_payloads() -> None:
    text = "가".encode("utf-16-le")
    extended = struct.pack("<H", 11) + b"\x00" * 14  # table anchor, 8 WCHARs
    tab = struct.pack("<H", 9) + b"\x00" * 14
    end = struct.pack("<H", 13)
    data = text + extended + text + tab + "나".encode("utf-16-le") + end
    assert decode_para_text(data) == "가가 나\n"


def test_decode_para_text_keeps_surrogate_pairs() -> None:
    assert decode_para_text("𠀀".encode("utf-16-le")) == "𠀀"


def test_parse_records_builds_tree() -> None:
    stream = _record(66, 0, b"h") + _record(67, 1, b"t") + _record(66, 0, b"h2")
    roots = parse_records(stream)
    assert [r.tag for r in roots] == [66, 66]
    assert [c.tag for c in roots[0].children] == [67]


@pytest.mark.parametrize(
    "stream",
    [
        b"\x01\x02",  # truncated header
        struct.pack("<I", 66 | (10 << 20)) + b"abc",  # size beyond stream
        _record(66, 0, b"") + _record(67, 2, b""),  # level skips a generation
        _record(67, 1, b""),  # child without parent
    ],
)
def test_parse_records_rejects_malformed_streams(stream: bytes) -> None:
    with pytest.raises(HwpFormatError):
        parse_records(stream)

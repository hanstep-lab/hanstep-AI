from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import pytest
from pypdf import PdfWriter

from app.rag.extractors import extract, extract_derived
from app.rag.extractors.base import normalize_text
from app.rag.manifest import SourceEntry, is_official_url, preflight
from app.rag.sources import discover, inspect_file, resolve_inside, sha256_bytes
from tests.conftest import DATA_ROOT

pytestmark = pytest.mark.unit

HP_NS = 'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"'


def _hwpx(section_xml: str, extra: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/hwp+zip")
        archive.writestr("Contents/section0.xml", section_xml)
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _para(text: str) -> str:
    return f"<hp:p><hp:run><hp:t>{text}</hp:t></hp:run></hp:p>"


def test_real_pdf_has_reliable_pages() -> None:
    data = (DATA_ROOT / "visa/selection/e74_selection_20260601.pdf").read_bytes()
    report = extract("pdf", data)
    assert report.status == "ok"
    assert report.pages == 25
    assert all(block.page_number for block in report.blocks)
    assert "33,000" in "\n".join(b.text for b in report.blocks)


def test_blank_pdf_needs_ocr_not_success() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    buffer = io.BytesIO()
    writer.write(buffer)
    report = extract("pdf", buffer.getvalue())
    assert report.status == "needs_ocr"
    assert report.blocks == []
    assert report.remedy


def test_corrupt_pdf_fails() -> None:
    report = extract("pdf", b"%PDF-1.7\nnot really a pdf")
    assert report.status in ("failed", "needs_ocr")
    assert report.blocks == []


def test_real_hwp_extracts_text_and_tables_without_pages() -> None:
    data = (DATA_ROOT / "visa/selection/e74_selection_2026_original.hwp").read_bytes()
    report = extract("hwp", data)
    assert report.status == "ok", report.reason
    assert report.extractor == "hwp5-records"
    assert report.tables > 0
    assert all(block.page_number is None for block in report.blocks)
    tables = [b.text for b in report.blocks if b.kind == "table"]
    assert any("K-point E74 |  | 33,000 | 12,000" in t for t in tables)


def test_derived_pdf_drops_page_numbers_and_records_hash() -> None:
    data = (DATA_ROOT / "visa/guides/e74_online_application_manual_2026.pdf").read_bytes()
    report = extract_derived("pdf", data)
    assert report.status == "ok"
    assert report.derived_sha256 == sha256_bytes(data)
    assert all(block.page_number is None for block in report.blocks)


def test_real_hwpx_keeps_tables_and_omits_pages() -> None:
    path = DATA_ROOT / "visa/recommendations/local/gyeonggi/e74_gyeonggi_20260601.hwpx"
    report = extract("hwpx", path.read_bytes())
    assert report.status == "ok"
    assert report.tables > 0
    assert all(block.page_number is None for block in report.blocks)
    text = "\n".join(b.text for b in report.blocks)
    assert "경기도 공고 제2026-5708호" in text
    assert "2,145" in text


def test_hwpx_paragraph_and_table_order() -> None:
    table = (
        "<hp:p><hp:run><hp:tbl>"
        "<hp:tr><hp:tc><hp:subList>" + _para("구분") + "</hp:subList></hp:tc>"
        "<hp:tc><hp:subList>" + _para("인원") + "</hp:subList></hp:tc></hp:tr>"
        "<hp:tr><hp:tc><hp:subList>" + _para("경기") + "</hp:subList></hp:tc>"
        "<hp:tc><hp:subList>" + _para("2,145") + "</hp:subList></hp:tc></hp:tr>"
        "</hp:tbl></hp:run></hp:p>"
    )
    xml = f"<hs:sec {HP_NS} xmlns:hs='x'>{_para('첫 문단')}{table}{_para('끝 문단')}</hs:sec>"
    report = extract("hwpx", _hwpx(xml))
    assert [b.kind for b in report.blocks] == ["paragraph", "table", "paragraph"]
    assert report.blocks[1].text == "구분 | 인원\n경기 | 2,145"
    assert report.blocks[1].table_header == "구분 | 인원"


def test_hwpx_rejects_xxe() -> None:
    xml = (
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
        f"<hs:sec {HP_NS} xmlns:hs='x'><hp:p><hp:run><hp:t>&e;</hp:t></hp:run></hp:p></hs:sec>"
    )
    report = extract("hwpx", _hwpx(xml))
    assert report.status == "failed"
    assert report.blocks == []


def test_hwpx_rejects_path_traversal_member() -> None:
    data = _hwpx(f"<hs:sec {HP_NS} xmlns:hs='x'>{_para('a')}</hs:sec>", {"../evil.txt": b"x"})
    report = extract("hwpx", data)
    assert report.status == "failed"
    assert "unsafe" in report.reason


def test_hwpx_rejects_zip_bomb_ratio() -> None:
    bomb = b"<hs:sec/>" + b" " * (5 * 1024 * 1024)
    data = _hwpx(f"<hs:sec {HP_NS} xmlns:hs='x'>{_para('a')}</hs:sec>", {"Contents/big.xml": bomb})
    report = extract("hwpx", data)
    assert report.status == "failed"
    assert "ratio" in report.reason


def test_normalization_preserves_meaningful_symbols() -> None:
    text, dropped = normalize_text("①  연봉 2,600만원 이상   (−5%, ㎡)\n- 3 -\n’26. 12. 31.")
    assert text == "① 연봉 2,600만원 이상 (−5%, ㎡)\n’26. 12. 31."
    assert dropped == 1


def test_resolve_inside_blocks_escape(tmp_path: Path) -> None:
    (tmp_path / "root").mkdir()
    with pytest.raises(ValueError):
        resolve_inside(tmp_path / "root", "../secret.pdf")
    with pytest.raises(ValueError):
        resolve_inside(tmp_path / "root", "/etc/passwd")


def test_discover_flags_symlink_escape_and_fake_extension(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-1.4 x")
    os.symlink(outside, root / "link.pdf")
    (root / "fake.pdf").write_bytes(b"PK\x03\x04 not a pdf")
    (root / ".gitkeep").write_bytes(b"")
    results = {r.relative_path: r for r in discover(root)}
    assert results["link.pdf"].status == "failed"
    assert results["fake.pdf"].status == "failed"
    assert results[".gitkeep"].status == "excluded"


def test_inventory_of_real_data_tracks_every_file() -> None:
    results = discover(DATA_ROOT)
    found = [r for r in results if r.status == "found"]
    assert len(found) == 10
    assert {r.detected_kind for r in found} == {"pdf", "hwp", "hwpx"}
    assert all(r.status in ("found", "excluded") for r in results)


def _entry(**overrides: object) -> SourceEntry:
    path = "visa/selection/e74_selection_20260601.pdf"
    base: dict[str, object] = {
        "path": path,
        "domain": "visa",
        "sha256": inspect_file(DATA_ROOT, path).sha256,
        "title": "2026년 숙련기능인력(E-7-4) 선발 계획",
        "source_url": "https://www.moj.go.kr/example",
        "source_org": "법무부",
        "visa_types": ["E-7-4"],
        "source_type": "notice",
        "policy_document_id": 1,
        "document_version": "v1",
        "review": {"verified": True},
    }
    base.update(overrides)
    return SourceEntry.model_validate(base)


def test_preflight_passes_complete_visa_entry() -> None:
    _, blockers = preflight(_entry(), DATA_ROOT)
    assert blockers == []


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"policy_document_id": None}, "policy_document_id"),
        ({"source_url": "https://github.com/hanstep-lab/x.pdf"}, "official"),
        ({"source_url": "http://www.moj.go.kr/x"}, "official"),
        ({"review": {"verified": False}}, "reviewed"),
        ({"sha256": "0" * 64}, "sha256"),
        ({"domain": "finance"}, "finance"),
        ({"visa_types": None}, "visa_types"),
        ({"source_type": None}, "source_type"),
    ],
)
def test_preflight_blocks_unconfirmed_values(overrides: dict[str, object], expected: str) -> None:
    _, blockers = preflight(_entry(**overrides), DATA_ROOT)
    assert any(expected in b for b in blockers), blockers


def test_official_url_rules() -> None:
    assert is_official_url("https://www.law.go.kr/x")
    assert not is_official_url("https://abc.r2.cloudflarestorage.com/x")
    assert not is_official_url("ftp://www.law.go.kr/x")


def test_source_type_is_required_only_for_visa_documents() -> None:
    path = "finance/shinhan/sol_global_savings.pdf"
    finance = _entry(
        path=path, domain="finance", sha256=inspect_file(DATA_ROOT, path).sha256, source_type=None
    )
    _, blockers = preflight(finance, DATA_ROOT)
    assert not any("source_type" in b for b in blockers)
    assert any("finance" in b for b in blockers)

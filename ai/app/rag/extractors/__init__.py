"""내용 스니핑으로 고르는 형식별 추출기(HWP는 절대 PDF 파서로 보내지 않는다)."""

from __future__ import annotations

from app.rag.extractors.base import Block, ExtractionReport
from app.rag.extractors.hwp import extract_hwp
from app.rag.extractors.hwpx import extract_hwpx
from app.rag.extractors.pdf import extract_pdf
from app.rag.sources import FileKind, sha256_bytes


def extract(kind: FileKind, data: bytes) -> ExtractionReport:
    if kind == "pdf":
        return extract_pdf(data)
    if kind == "hwpx":
        return extract_hwpx(data)
    if kind == "hwp":
        return extract_hwp(data)
    return ExtractionReport("failed", "none", reason=f"unsupported file kind: {kind}")


def extract_derived(kind: FileKind, data: bytes) -> ExtractionReport:
    """사람이 만든 파생본에서 추출한다. 변환본의 페이지 번호는 원본의 페이지로 신뢰하지
    않으므로 버린다."""
    if kind == "pdf":
        report = extract_pdf(data, page_numbers_reliable=False)
    elif kind == "hwpx":
        report = extract_hwpx(data)
    elif kind == "hwp":
        report = extract_hwp(data)
    else:
        return ExtractionReport("failed", "none", reason=f"unsupported derivative kind: {kind}")
    report.derived_sha256 = sha256_bytes(data)
    return report


__all__ = ["Block", "ExtractionReport", "extract", "extract_derived"]

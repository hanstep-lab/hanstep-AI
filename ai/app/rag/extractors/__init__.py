"""Format-specific extractors, chosen by sniffed content (HWP never goes to the PDF parser)."""

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
    """Extract a human-made derivative. Page numbers of a conversion are not trusted as the
    original's pages, so they are dropped."""
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

"""페이지 추적과 스캔본·깨진 글자 감지를 포함한 PDF 텍스트 추출."""

from __future__ import annotations

import io

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.rag.extractors.base import Block, ExtractionReport, garbled_ratio, normalize_text

# 페이지당 평균 글자 수가 이보다 적으면 PDF를 스캔본으로 본다.
MIN_CHARS_PER_PAGE = 40
MAX_GARBLED_RATIO = 0.05
OCR_REMEDY = (
    "Run OCR with a reviewed tool, record tool/version/settings, and register the OCR output "
    "as a derived_inputs entry (kind=ocr) in the manifest."
)


def extract_pdf(data: bytes, *, page_numbers_reliable: bool = True) -> ExtractionReport:
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            return ExtractionReport("failed", "pypdf", reason="encrypted PDF")
        page_texts = [page.extract_text() or "" for page in reader.pages]
    except (PdfReadError, ValueError, KeyError, OSError) as exc:
        return ExtractionReport("failed", "pypdf", reason=f"unreadable PDF: {exc}")

    report = ExtractionReport(
        "ok", "pypdf", pages=len(page_texts), page_numbers_reliable=page_numbers_reliable
    )
    if not page_texts:
        report.status, report.reason = "failed", "PDF has no pages"
        return report

    all_text: list[str] = []
    for number, raw in enumerate(page_texts, start=1):
        text, dropped = normalize_text(raw)
        report.dropped_footer_lines += dropped
        if not text:
            report.empty_pages.append(number)
            continue
        all_text.append(text)
        for paragraph in _split_paragraphs(text):
            report.blocks.append(
                Block(paragraph, page_number=number if page_numbers_reliable else None)
            )
    joined = "\n".join(all_text)
    report.paragraphs = len(report.blocks)
    report.characters = len(joined)
    report.garbled_ratio = garbled_ratio(joined)

    if report.characters < MIN_CHARS_PER_PAGE * len(page_texts):
        report.status = "needs_ocr"
        report.reason = f"only {report.characters} characters over {len(page_texts)} pages"
        report.remedy = OCR_REMEDY
        report.blocks = []
    elif report.garbled_ratio > MAX_GARBLED_RATIO:
        report.status = "failed"
        report.reason = f"garbled character ratio {report.garbled_ratio:.2%}"
        report.remedy = OCR_REMEDY
        report.blocks = []
    return report


def _split_paragraphs(text: str) -> list[str]:
    """빈 줄에서 나눈다. pypdf는 빈 줄을 거의 내지 않아 대부분 페이지가 블록 하나로 남는다.

    더 세밀한 구조(제목, 항목)는 청커의 줄 단위 분할이 처리한다.
    """
    return [part.strip() for part in text.split("\n\n") if part.strip()]

"""모든 추출기가 공유하는 추출 결과 타입과 텍스트 정규화."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

ExtractionStatus = Literal["ok", "needs_ocr", "needs_conversion", "failed"]
BlockKind = Literal["paragraph", "table"]

# "- 1 -" 같은 페이지 바닥글에는 내용이 없다.
_PAGE_FOOTER = re.compile(r"^\s*-\s*\d+\s*-\s*$")
_INLINE_SPACE = re.compile(r"[ \t 　]+")
# 대체 문자나 사용자 정의 영역(private-use) 글리프는 폰트 매핑이 깨졌다는 신호다.
_GARBLED = re.compile(r"[�-]")


def normalize_line(line: str) -> str:
    """NFC 정규화와 공백 압축만 한다.

    NFKC는 일부러 피한다. 정책 문서에서 의미가 있는 원문자 숫자, 단위(㎡), 전각 기호를
    바꿔 버리기 때문이다.
    """
    return _INLINE_SPACE.sub(" ", unicodedata.normalize("NFC", line)).strip()


def normalize_text(text: str) -> tuple[str, int]:
    """페이지/문단을 정규화한다. 텍스트와 버린 바닥글 줄 수를 반환한다."""
    lines: list[str] = []
    dropped = 0
    for raw in text.splitlines():
        line = normalize_line(raw)
        if _PAGE_FOOTER.match(line):
            dropped += 1
            continue
        lines.append(line)
    # 연속된 빈 줄을 문단 구분 하나로 합친다.
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return out, dropped


def garbled_ratio(text: str) -> float:
    visible = [c for c in text if not c.isspace()]
    if not visible:
        return 0.0
    return len(_GARBLED.findall(text)) / len(visible)


@dataclass(frozen=True)
class Block:
    """읽는 순서상 이어진 원문 텍스트 단위."""

    text: str
    kind: BlockKind = "paragraph"
    page_number: int | None = None
    # 표일 때의 헤더 행. 긴 표를 나눌 때 반복한다.
    table_header: str | None = None


@dataclass
class ExtractionReport:
    """파일 하나의 추출 결과: 블록, 통계, 상태와 실패 사유."""

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
    # 텍스트가 파생본(변환/OCR)에서 나왔을 때 그 파생본의 해시. 공식 해시는 원본
    # 파일의 해시로 유지한다.
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

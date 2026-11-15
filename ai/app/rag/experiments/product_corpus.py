"""금융상품 설명서·약관 PDF 말뭉치와 평가 문항: 추출, 문서 제목 매핑, 정답 근거 확인.

추출은 운영과 같은 extractors를 쓴다. 문서는 PDF 첫 줄의 제목('…상품설명서'/'…약관')으로
이름을 정하고, 바이트가 같은 PDF는 한 문서로 합친다(파일 이름만 다른 사본).
평가 문항은 고치지 않는다. 정답 문서나 근거 문장을 찾지 못한 문항은 invalid로 표시만 한다.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app.rag.extractors import Block, extract

DocumentKind = Literal["product", "terms"]

_PRODUCT_SUFFIX = "상품설명서"
_TERMS_SUFFIX = "약관"
# 『신한 안녕, 반가워 적금』 상품설명서처럼 상품명을 감싼 괄호.
_NAME_WRAPPERS = str.maketrans("", "", "『』「」")
_WHITESPACE = re.compile(r"\s+")

QUESTION_FIELDS: dict[str, type] = {
    "id": str,
    "question": str,
    "goldDocument": str,
    "goldQuote": str,
    "page": int,
    "namesProduct": bool,
}


class CorpusError(ValueError):
    """말뭉치나 평가 파일을 실험에 쓸 수 없다."""


def match_key(text: str) -> str:
    """공백·줄바꿈을 모두 지운 NFC 문자열. PDF 추출의 줄바꿈 위치·띄어쓰기 차이를 무시한다."""
    return _WHITESPACE.sub("", unicodedata.normalize("NFC", text))


def parse_title(first_line: str) -> tuple[str, DocumentKind]:
    """문서 첫 줄 제목에서 (문서 이름, 종류)를 얻는다.

    '…상품설명서'는 접미어를 뗀 상품명, '…약관'은 제목 그대로를 이름으로 쓴다.
    """
    title = _WHITESPACE.sub(" ", first_line.translate(_NAME_WRAPPERS)).strip()
    if title.endswith(_PRODUCT_SUFFIX):
        name = title.removesuffix(_PRODUCT_SUFFIX).strip()
        kind: DocumentKind = "product"
    elif title.endswith(_TERMS_SUFFIX):
        name, kind = title, "terms"
    else:
        raise CorpusError(f"title is not '…{_PRODUCT_SUFFIX}' or '…{_TERMS_SUFFIX}': {title!r}")
    if not name:
        raise CorpusError(f"empty document name in title {title!r}")
    return name, kind


@dataclass(frozen=True)
class ProductDocument:
    """말뭉치의 문서 하나. 바이트가 같은 파일들은 ``files``에 함께 남는다."""

    doc_id: str
    name: str
    title: str
    kind: DocumentKind
    sha256: str
    files: tuple[str, ...]
    pages: int | None
    blocks: tuple[Block, ...]

    @property
    def text(self) -> str:
        return "\n".join(block.text for block in self.blocks)

    def summary(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "name": self.name,
            "title": self.title,
            "kind": self.kind,
            "files": list(self.files),
            "pages": self.pages,
            "characters": len(self.text),
        }


def build_document(files: Sequence[str], data: bytes) -> ProductDocument:
    report = extract("pdf", data)
    if report.status != "ok" or not report.blocks:
        raise CorpusError(f"{files[0]}: extraction {report.status} ({report.reason})")
    title = report.blocks[0].text.split("\n", 1)[0].strip()
    name, kind = parse_title(title)
    digest = hashlib.sha256(data).hexdigest()
    return ProductDocument(
        doc_id=f"doc-{digest[:12]}",
        name=name,
        title=title,
        kind=kind,
        sha256=digest,
        files=tuple(files),
        pages=report.pages,
        blocks=tuple(report.blocks),
    )


def load_corpus(directory: Path) -> list[ProductDocument]:
    """``directory``의 PDF를 추출해 이름순 문서 목록을 만든다."""
    paths = sorted(directory.glob("*.pdf"))
    if not paths:
        raise CorpusError(f"no PDF files in {directory}")
    by_hash: dict[str, list[Path]] = {}
    for path in paths:
        by_hash.setdefault(hashlib.sha256(path.read_bytes()).hexdigest(), []).append(path)
    documents = [
        build_document([p.name for p in group], group[0].read_bytes()) for group in by_hash.values()
    ]
    # 내용이 다른 두 문서가 같은 이름이면 정답 문서를 하나로 정할 수 없다.
    seen: dict[str, ProductDocument] = {}
    for document in documents:
        other = seen.setdefault(match_key(document.name), document)
        if other is not document:
            raise CorpusError(
                f"documents {other.files} and {document.files} share name {document.name!r}"
            )
    return sorted(documents, key=lambda d: d.name)


@dataclass(frozen=True)
class Question:
    """평가 문항 하나(평가 파일 그대로)."""

    id: str
    question: str
    gold_document: str
    gold_quote: str
    page: int
    names_product: bool


def load_questions(path: Path) -> tuple[str, list[Question]]:
    """평가 파일 ``{version, questions[]}``을 읽어 형식을 검증한다. 형식이 틀리면 CorpusError."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        raise CorpusError(f"{path}: expected an object with a 'questions' list")
    version = data.get("version")
    if not isinstance(version, str) or not version:
        raise CorpusError(f"{path}: 'version' must be a non-empty string")
    questions: list[Question] = []
    for index, raw in enumerate(data["questions"]):
        if not isinstance(raw, dict) or set(raw) != set(QUESTION_FIELDS):
            fields = sorted(QUESTION_FIELDS)
            raise CorpusError(f"{path}: question #{index} must have fields {fields}")
        for name, expected in QUESTION_FIELDS.items():
            value = raw[name]
            # bool은 int의 하위 타입이라 page에 true가 들어오면 따로 막는다.
            if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
                raise CorpusError(f"{path}: question #{index} field {name!r} must be {expected}")
        questions.append(
            Question(
                id=raw["id"],
                question=raw["question"],
                gold_document=raw["goldDocument"],
                gold_quote=raw["goldQuote"],
                page=raw["page"],
                names_product=raw["namesProduct"],
            )
        )
    ids = [q.id for q in questions]
    if len(ids) != len(set(ids)):
        raise CorpusError(f"{path}: duplicate question ids")
    return version, questions


@dataclass(frozen=True)
class GoldCheck:
    """문항의 정답 문서 매핑과 근거 문장 확인 결과."""

    question_id: str
    doc_id: str | None
    valid: bool
    reason: str = ""
    # 정규화 후 근거 문장이 들어 있는 페이지(참고용, 페이지 경계에 걸치면 비어 있다).
    quote_pages: tuple[int, ...] = ()


def check_gold(
    questions: Sequence[Question], documents: Sequence[ProductDocument]
) -> list[GoldCheck]:
    """goldDocument → doc id, goldQuote가 그 문서 추출 텍스트에 있는지 확인한다."""
    by_name = {match_key(d.name): d for d in documents}
    checks: list[GoldCheck] = []
    for question in questions:
        document = by_name.get(match_key(question.gold_document))
        if document is None:
            checks.append(GoldCheck(question.id, None, False, "goldDocument not in corpus"))
            continue
        quote = match_key(question.gold_quote)
        if quote not in match_key(document.text):
            checks.append(
                GoldCheck(question.id, document.doc_id, False, "goldQuote not in document text")
            )
            continue
        pages: dict[int, list[str]] = {}
        for block in document.blocks:
            if block.page_number is not None:
                pages.setdefault(block.page_number, []).append(block.text)
        found = tuple(
            sorted(page for page, texts in pages.items() if quote in match_key("".join(texts)))
        )
        checks.append(GoldCheck(question.id, document.doc_id, True, quote_pages=found))
    return checks

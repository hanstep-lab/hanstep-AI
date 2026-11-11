"""파이프라인 입력 매니페스트(``specs/rag/sources.json``).

매니페스트는 MongoDB 스키마가 아니라 이 파이프라인의 입력 계약이다. 검증되지 않은 값은
``null``로 두며, 확인될 때까지 :func:`preflight`가 적재를 막는다.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from app.rag.schemas import SourceType
from app.rag.sources import DiscoveredFile, inspect_file

Domain = Literal["visa", "finance"]

# 공식 출처가 아니라 사본을 보관하는 호스트. ``source_url``로 절대 받지 않는다.
NON_OFFICIAL_HOSTS = ("github.com", "githubusercontent.com", "r2.cloudflarestorage.com", "r2.dev")


class DerivedInput(BaseModel):
    """텍스트 추출에 쓰는, 사람이나 도구가 만든 파생본(예: HWP -> PDF)."""

    model_config = ConfigDict(extra="forbid")

    path: str
    kind: Literal["pdf_conversion", "ocr"]
    sha256: str | None = None
    tool: str | None = None
    created_by: str | None = None
    note: str | None = None


class Review(BaseModel):
    """공식 출처 검토 여부와 검토자·검토일 기록."""

    model_config = ConfigDict(extra="forbid")

    verified: bool = False
    verified_by: str | None = None
    verified_at: date | None = None
    # 미검증 값의 출처와 아직 확인이 필요한 사항.
    note: str | None = None


class Relations(BaseModel):
    """이 파일과 다른 원본 파일 사이의 파생·대체 관계."""

    model_config = ConfigDict(extra="forbid")

    # 이 파일이 파생된 원본의 상대 경로(예: PDF의 HWP 원본).
    derived_from: str | None = None
    supersedes: str | None = None
    note: str | None = None


class SourceEntry(BaseModel):
    """매니페스트에 등록된 원본 파일 하나와 그 메타데이터."""

    model_config = ConfigDict(extra="forbid")

    path: str
    # 출처가 게시한 그대로의 파일명. 추적용으로 보관한다(저장 경로는 정규화된다).
    original_filename: str | None = None
    domain: Domain
    classification_hint: str | None = None
    sha256: str
    title: str | None = None
    source_url: str | None = None
    # 내려받은 바이트를 이 파일과 해시로 대조한 직접 파일 URL(출처 증명, 재확인용).
    download_url: str | None = None
    source_org: str | None = None
    visa_types: list[str] | None = None
    # 소문자로 바꾼 MySQL policy_documents.source_type
    # (law/admin_rule/notice/guide/score_table).
    source_type: SourceType | None = None
    # MySQL published_on(발행일). 공식 출처에 명시된 경우에만 넣는다.
    issued_on: date | None = None
    policy_document_id: int | None = Field(default=None, ge=1)
    document_version: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    review: Review = Field(default_factory=Review)
    relations: Relations = Field(default_factory=Relations)
    derived_inputs: list[DerivedInput] = Field(default_factory=list)


class Manifest(BaseModel):
    """데이터 루트와 원본 항목 목록을 담은 매니페스트 전체."""

    model_config = ConfigDict(extra="forbid")

    manifest_version: Literal[1] = 1
    data_root: str = "../data/rag_documents"
    sources: list[SourceEntry]

    def resolve_root(self, manifest_path: Path) -> Path:
        return (manifest_path.parent / self.data_root).resolve()


def load_manifest(path: Path) -> Manifest:
    return Manifest.model_validate(json.loads(path.read_text(encoding="utf-8")))


def is_official_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    return not any(host == bad or host.endswith("." + bad) for bad in NON_OFFICIAL_HOSTS)


def preflight(entry: SourceEntry, root: Path) -> tuple[DiscoveredFile, list[str]]:
    """항목 하나를 디스크와 확정된 계약에 대조해 검사한다.

    파일 검사 결과와 차단 사유 목록을 반환한다. 목록이 비어 있으면 적재해도 된다는
    뜻이다(단, apply 시점의 MySQL gateway 검사는 여전히 거친다).
    """
    found = inspect_file(root, entry.path)
    blockers: list[str] = []
    if found.status != "found":
        blockers.append(f"file {found.status}: {found.reason}")
    elif found.sha256 != entry.sha256:
        blockers.append("sha256 on disk differs from manifest; register a new version")

    if entry.domain == "finance":
        blockers.append(
            "finance storage contract unconfirmed (collection, product id, approval, versioning)"
        )
    if not entry.review.verified:
        blockers.append("official source not reviewed (review.verified=false)")
    required = ["title", "source_org", "document_version"]
    if entry.domain == "visa":
        # policy_documents.source_type. finance는 대신 product_documents.doc_kind를 쓴다.
        required.append("source_type")
    for name in required:
        if not getattr(entry, name):
            blockers.append(f"{name} unconfirmed")
    if entry.policy_document_id is None:
        blockers.append("policy_document_id unconfirmed (must come from MySQL policy_documents)")
    if not entry.source_url:
        blockers.append("source_url unconfirmed")
    elif not is_official_url(entry.source_url):
        blockers.append("source_url must be an official https URL, not a GitHub/R2 copy")
    if entry.domain == "visa" and not entry.visa_types:
        blockers.append("visa_types unconfirmed")
    if entry.effective_from and entry.effective_to and entry.effective_from > entry.effective_to:
        blockers.append("effective_from is after effective_to")
    for derived in entry.derived_inputs:
        result = inspect_file(root, derived.path)
        if result.status != "found":
            blockers.append(f"derived input {derived.path}: {result.status} {result.reason}")
        elif derived.sha256 and derived.sha256 != result.sha256:
            blockers.append(f"derived input {derived.path}: sha256 mismatch")
    return found, blockers

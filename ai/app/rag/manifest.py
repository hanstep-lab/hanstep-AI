"""Pipeline input manifest (``specs/rag/sources.json``).

The manifest is an input contract for this pipeline, not a MongoDB schema. Unverified values
stay ``null`` and :func:`preflight` blocks loading until they are confirmed.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from app.rag.sources import DiscoveredFile, inspect_file

Domain = Literal["visa", "finance"]

# Hosts that hold copies, not official sources; never accept them as ``source_url``.
NON_OFFICIAL_HOSTS = ("github.com", "githubusercontent.com", "r2.cloudflarestorage.com", "r2.dev")


class DerivedInput(BaseModel):
    """A human or tool generated derivative (e.g. HWP -> PDF) used for text extraction."""

    model_config = ConfigDict(extra="forbid")

    path: str
    kind: Literal["pdf_conversion", "ocr"]
    sha256: str | None = None
    tool: str | None = None
    created_by: str | None = None
    note: str | None = None


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verified: bool = False
    verified_by: str | None = None
    verified_at: date | None = None


class Relations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Relative path of the original this file was derived from (e.g. HWP original of a PDF).
    derived_from: str | None = None
    supersedes: str | None = None
    note: str | None = None


class SourceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    domain: Domain
    classification_hint: str | None = None
    sha256: str
    title: str | None = None
    source_url: str | None = None
    source_org: str | None = None
    visa_types: list[str] | None = None
    policy_document_id: int | None = Field(default=None, ge=1)
    document_version: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    review: Review = Field(default_factory=Review)
    relations: Relations = Field(default_factory=Relations)
    derived_inputs: list[DerivedInput] = Field(default_factory=list)


class Manifest(BaseModel):
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
    """Check one entry against disk and the confirmed contract.

    Returns the file inspection result and a list of blockers. An empty list means the entry
    may be loaded (still subject to the MySQL gateway check at apply time).
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
    for name in ("title", "source_org", "document_version"):
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

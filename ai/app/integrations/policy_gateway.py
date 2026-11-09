"""Boundary to the MySQL source of truth (``policy_documents``, ``ai_jobs``).

The backend contract (Spring internal API paths, DTOs, auth header, status enums) is not
confirmed. :class:`UnconfiguredPolicyGateway` fails closed with the exact missing items;
:class:`InMemoryPolicyGateway` exists for tests only. Do not add an HTTP implementation with
guessed URLs.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal, Protocol

from app.rag.errors import ContractBlockedError

# MySQL policy_documents.review_status (BE schema, API spec: ENUM values as-is).
MysqlReviewStatus = Literal["DRAFT", "APPROVED", "NEEDS_REVIEW", "RETIRED"]

MISSING_POLICY_CONTRACT = (
    "MySQL policy_documents contract unconfirmed: need Spring internal API method/path for "
    "document lookup and registration, auth header name, response DTO (id, document_version, "
    "source_sha256, review_status, current/active flag, R2 key), and ID/version issuance rules"
)
MISSING_JOB_CONTRACT = (
    "MySQL ai_jobs contract unconfirmed: need job create/status API, status enum, idempotency "
    "key, retry and progress fields"
)


@dataclass(frozen=True)
class PolicyDocumentRecord:
    """One ``policy_documents`` row. Each version is its own row (``content_hash`` is unique)."""

    policy_document_id: int
    policy_version: str
    content_hash: str
    review_status: MysqlReviewStatus
    # BE's verdict: APPROVED and inside the effective period, by BE rules (R1 search-eligibility).
    searchable: bool = False
    object_key: str | None = None


class PolicyDocumentGateway(Protocol):
    def get_version(self, policy_document_id: int, document_version: str) -> PolicyDocumentRecord:
        """Raise ``LookupError`` when the version is not registered."""
        ...

    def current_versions(self, policy_document_ids: Iterable[int]) -> dict[int, str]:
        """Searchable version per document (absent = not searchable). Must raise on failure."""
        ...

    def record_r2_object(
        self, policy_document_id: int, document_version: str, object_key: str
    ) -> None: ...


class JobReporter(Protocol):
    def report(self, job_id: str, payload: dict[str, object]) -> None: ...


class UnconfiguredPolicyGateway:
    def get_version(self, policy_document_id: int, document_version: str) -> PolicyDocumentRecord:
        raise ContractBlockedError(MISSING_POLICY_CONTRACT)

    def current_versions(self, policy_document_ids: Iterable[int]) -> dict[int, str]:
        raise ContractBlockedError(MISSING_POLICY_CONTRACT)

    def record_r2_object(
        self, policy_document_id: int, document_version: str, object_key: str
    ) -> None:
        raise ContractBlockedError(MISSING_POLICY_CONTRACT)


class UnconfiguredJobReporter:
    def report(self, job_id: str, payload: dict[str, object]) -> None:
        raise ContractBlockedError(MISSING_JOB_CONTRACT)


@dataclass
class InMemoryPolicyGateway:
    """Test double. Keys are (policy_document_id, policy_version)."""

    records: dict[tuple[int, str], PolicyDocumentRecord] = field(default_factory=dict)
    r2_keys: dict[tuple[int, str], str] = field(default_factory=dict)
    fail: bool = False

    def add(self, record: PolicyDocumentRecord) -> None:
        self.records[(record.policy_document_id, record.policy_version)] = record

    def get_version(self, policy_document_id: int, document_version: str) -> PolicyDocumentRecord:
        if self.fail:
            raise ConnectionError("policy gateway unavailable")
        try:
            return self.records[(policy_document_id, document_version)]
        except KeyError as exc:
            raise LookupError(f"{policy_document_id}/{document_version} not registered") from exc

    def current_versions(self, policy_document_ids: Iterable[int]) -> dict[int, str]:
        if self.fail:
            raise ConnectionError("policy gateway unavailable")
        wanted = set(policy_document_ids)
        return {
            r.policy_document_id: r.policy_version
            for r in self.records.values()
            if r.policy_document_id in wanted and r.searchable and r.review_status == "APPROVED"
        }

    def record_r2_object(
        self, policy_document_id: int, document_version: str, object_key: str
    ) -> None:
        if self.fail:
            raise ConnectionError("policy gateway unavailable")
        self.r2_keys[(policy_document_id, document_version)] = object_key


@dataclass
class InMemoryJobReporter:
    reports: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def report(self, job_id: str, payload: dict[str, object]) -> None:
        self.reports.append((job_id, payload))

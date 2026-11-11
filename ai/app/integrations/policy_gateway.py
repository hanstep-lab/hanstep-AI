"""기준 데이터인 MySQL(``policy_documents``, ``ai_jobs``)과의 경계.

백엔드 계약(Spring 내부 API 경로, DTO, 인증 헤더, 상태 enum)은 아직 확정되지 않았다.
:class:`UnconfiguredPolicyGateway`는 빠진 항목을 정확히 알리며 차단한다(fail-closed).
:class:`InMemoryPolicyGateway`는 테스트 전용이다. 추측한 URL로 HTTP 구현을 추가하지 않는다.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from app.rag.errors import ContractBlockedError

# MySQL policy_documents.review_status(BE 스키마, API 명세: ENUM 값 그대로).
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
    """``policy_documents`` 한 행. 버전마다 따로 행이 있다(``content_hash``는 유일하다)."""

    policy_document_id: int
    policy_version: str
    content_hash: str
    review_status: MysqlReviewStatus
    # BE 판정값: BE 규칙에 따라 APPROVED이면서 적용 기간 안인지(R1 search-eligibility).
    searchable: bool = False
    object_key: str | None = None


class PolicyDocumentGateway(Protocol):
    """MySQL ``policy_documents`` 경계. 적재는 버전 확인에, 검색은 승인 확인에 쓴다."""

    def get_version(self, policy_document_id: int, document_version: str) -> PolicyDocumentRecord:
        """등록되지 않은 버전이면 ``LookupError``를 던진다."""
        ...

    def current_versions(self, policy_document_ids: Iterable[int]) -> dict[int, str]:
        """문서별 검색 가능 버전(없으면 검색 불가). 실패하면 반드시 예외를 던진다."""
        ...

    def record_r2_object(
        self, policy_document_id: int, document_version: str, object_key: str
    ) -> None: ...


class JobReporter(Protocol):
    """MySQL ``ai_jobs`` 작업 상태 보고 인터페이스."""

    def report(self, job_id: str, payload: dict[str, object]) -> None: ...


class UnconfiguredPolicyGateway:
    """계약 미확정 시 쓰는 기본 게이트웨이. 모든 호출에서 ContractBlockedError를 낸다."""

    def get_version(self, policy_document_id: int, document_version: str) -> PolicyDocumentRecord:
        raise ContractBlockedError(MISSING_POLICY_CONTRACT)

    def current_versions(self, policy_document_ids: Iterable[int]) -> dict[int, str]:
        raise ContractBlockedError(MISSING_POLICY_CONTRACT)

    def record_r2_object(
        self, policy_document_id: int, document_version: str, object_key: str
    ) -> None:
        raise ContractBlockedError(MISSING_POLICY_CONTRACT)


class ManifestPolicyGateway:
    """스크립트 적재용 1차 게이트웨이(BE 명세 INT-13 비고: "1차는 스크립트로 직접 적재").

    사람이 MySQL에서 manifest로 옮겨 적은 ``policy_document_id`` / ``document_version`` /
    ``sha256``을 믿는다(``review.verified=true``). 검색 시점 승인 확인에는 답할 수 없으므로
    ``current_versions``는 막혀 있고 검색은 계속 차단된다(fail-closed).
    """

    def __init__(self, entries: Iterable[Any]) -> None:
        self._records: dict[tuple[int, str], PolicyDocumentRecord] = {}
        self.r2_keys: dict[tuple[int, str], str] = {}
        for entry in entries:
            if entry.policy_document_id is None or not entry.document_version:
                continue
            if not entry.review.verified:
                continue
            record = PolicyDocumentRecord(
                entry.policy_document_id, entry.document_version, entry.sha256, "DRAFT"
            )
            self._records[(record.policy_document_id, record.policy_version)] = record

    def get_version(self, policy_document_id: int, document_version: str) -> PolicyDocumentRecord:
        try:
            return self._records[(policy_document_id, document_version)]
        except KeyError as exc:
            raise LookupError(
                f"{policy_document_id}/{document_version} not in verified manifest entries"
            ) from exc

    def current_versions(self, policy_document_ids: Iterable[int]) -> dict[int, str]:
        raise ContractBlockedError(
            "search-time approval check needs the Spring search-eligibility API (R1)"
        )

    def record_r2_object(
        self, policy_document_id: int, document_version: str, object_key: str
    ) -> None:
        # 청크를 저장한 뒤 INT-13으로 Spring에 보고한다.
        self.r2_keys[(policy_document_id, document_version)] = object_key


class UnconfiguredJobReporter:
    """계약 미확정 시 쓰는 기본 작업 보고기. 항상 ContractBlockedError를 낸다."""

    def report(self, job_id: str, payload: dict[str, object]) -> None:
        raise ContractBlockedError(MISSING_JOB_CONTRACT)


@dataclass
class InMemoryPolicyGateway:
    """테스트 대역. 키는 (policy_document_id, policy_version)이다."""

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
    """테스트용 작업 보고기. 받은 보고를 순서대로 쌓는다."""

    reports: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def report(self, job_id: str, payload: dict[str, object]) -> None:
        self.reports.append((job_id, payload))

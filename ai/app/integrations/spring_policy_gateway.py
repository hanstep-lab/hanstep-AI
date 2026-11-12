"""Spring을 통한 검색 시점 승인 확인(AI-R1 search-eligibility).

``GET /internal/v1/ai-context/policy-documents/search-eligibility?ids=...``는 존재하는
문서마다 MySQL ``policyVersion``과 BE가 정한 ``searchable`` 값을 돌려준다. 없는 id는
응답에서 빠질 뿐이다. 어떤 실패든 예외를 던져 검색이 차단되게 한다(fail-closed, 검증되지
않은 결과 없음).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import httpx

from app.integrations.policy_gateway import PolicyDocumentRecord
from app.rag.errors import ContractBlockedError

PATH = "/internal/v1/ai-context/policy-documents/search-eligibility"
MAX_IDS = 20
REVIEW_STATUSES = {"DRAFT", "APPROVED", "NEEDS_REVIEW", "RETIRED"}


class SpringGatewayError(Exception):
    """Spring에서 쓸 수 있는 응답을 받지 못했다. 호출자는 차단해야 한다(fail-closed)."""


class SpringPolicyGateway:
    """Spring search-eligibility API(AI-R1)로 검색 결과의 승인 상태를 확인한다."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        header: str = "X-Internal-Token",
        client: httpx.Client | None = None,
        timeout_s: float = 2.0,
    ) -> None:
        if not base_url or not api_key:
            raise ContractBlockedError(
                "SPRING_INTERNAL_API_URL / SPRING_INTERNAL_API_KEY not configured"
            )
        self._headers = {header: api_key}
        # 타임아웃을 짧게 둔다. 모든 검색 요청 안에서 실행되기 때문이다.
        self._client = client or httpx.Client(
            base_url=base_url, timeout=httpx.Timeout(timeout_s), follow_redirects=False
        )

    def close(self) -> None:
        self._client.close()

    def eligibility(self, policy_document_ids: Iterable[int]) -> list[dict[str, Any]]:
        ids = sorted({int(i) for i in policy_document_ids})
        if not ids:
            return []
        if len(ids) > MAX_IDS:
            raise SpringGatewayError(f"at most {MAX_IDS} ids per call")
        try:
            response = self._client.get(
                PATH, params={"ids": ",".join(map(str, ids))}, headers=self._headers
            )
        except httpx.HTTPError as exc:
            raise SpringGatewayError(
                f"search-eligibility unreachable: {type(exc).__name__}"
            ) from exc
        if response.status_code != 200:
            raise SpringGatewayError(f"search-eligibility returned HTTP {response.status_code}")
        try:
            documents = response.json()["documents"]
            if not isinstance(documents, list):
                raise TypeError("documents is not a list")
            for doc in documents:
                doc_id = doc.get("policyDocumentId")
                if isinstance(doc_id, bool) or not isinstance(doc_id, int):
                    raise TypeError("policyDocumentId is not an integer")
                if not isinstance(doc.get("searchable"), bool):
                    raise TypeError("searchable is not a boolean")
                if doc.get("reviewStatus") not in REVIEW_STATUSES:
                    raise TypeError("unknown reviewStatus")
        except (ValueError, KeyError, TypeError) as exc:
            raise SpringGatewayError("search-eligibility response has an unexpected shape") from exc
        requested = set(ids)
        return [doc for doc in documents if doc["policyDocumentId"] in requested]

    # --- PolicyDocumentGateway --------------------------------------------------------

    def current_versions(self, policy_document_ids: Iterable[int]) -> dict[int, str]:
        """문서별 검색 가능 버전. 없으면 "보여 주지 않음"을 뜻한다.

        Spring은 한 번에 ``MAX_IDS``개까지 받으므로 나눠 부른다. 하나라도 실패하면 전체가 실패한다.
        """
        ids = sorted({int(i) for i in policy_document_ids})
        current: dict[int, str] = {}
        for start in range(0, len(ids), MAX_IDS):
            for doc in self.eligibility(ids[start : start + MAX_IDS]):
                if doc["searchable"] and doc.get("policyVersion"):
                    current[doc["policyDocumentId"]] = str(doc["policyVersion"])
        return current

    def get_version(self, policy_document_id: int, document_version: str) -> PolicyDocumentRecord:
        # AI-R1에는 content_hash가 없어 원본 파일을 검증할 수 없다. 적재는 계속 manifest
        # 게이트웨이를 쓴다(BE 명세 INT-13 비고: 1차 스크립트 적재).
        raise ContractBlockedError(
            "Spring has no document lookup with content_hash; use ManifestPolicyGateway"
        )

    def record_r2_object(
        self, policy_document_id: int, document_version: str, object_key: str
    ) -> None:
        # 객체 키는 대신 INT-13 적재 완료 보고에 실어 보낸다.
        raise ContractBlockedError("object keys are reported through INT-13")

"""Search-time approval check against Spring (AI-R1 search-eligibility).

``GET /internal/v1/ai-context/policy-documents/search-eligibility?ids=...`` returns, per
existing document, the MySQL ``policyVersion`` and the BE-decided ``searchable`` flag. Missing
ids are simply absent. Any failure raises so retrieval fails closed (no unverified results).
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
    """Spring did not give a usable answer; callers must fail closed."""


class SpringPolicyGateway:
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
        # Short timeout: this runs inside every search request.
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
        """Searchable version per document; absent means "do not show"."""
        return {
            doc["policyDocumentId"]: str(doc["policyVersion"])
            for doc in self.eligibility(policy_document_ids)
            if doc["searchable"] and doc.get("policyVersion")
        }

    def get_version(self, policy_document_id: int, document_version: str) -> PolicyDocumentRecord:
        # AI-R1 carries no content_hash, so it cannot verify a source file; ingestion keeps
        # using the manifest gateway (BE spec INT-13 note: phase-1 script loading).
        raise ContractBlockedError(
            "Spring has no document lookup with content_hash; use ManifestPolicyGateway"
        )

    def record_r2_object(
        self, policy_document_id: int, document_version: str, object_key: str
    ) -> None:
        # The object key travels with the INT-13 ingested report instead.
        raise ContractBlockedError("object keys are reported through INT-13")

"""Vector search over approved, active chunks with a fail-closed MySQL version check."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pymongo.collection import Collection

from app.integrations.policy_gateway import PolicyDocumentGateway
from app.rag.embeddings import Embedder
from app.rag.errors import ContractBlockedError, ValidationFailedError

_VISA_TYPE = re.compile(r"^[A-Z]-\d{1,2}(?:-\d{1,2})?$")
MAX_VISA_TYPES = 10


class SearchUnavailableError(Exception):
    """Approval state could not be verified; no candidates may be returned."""


def validate_visa_types(values: list[Any] | None) -> list[str]:
    if not values:
        return []
    if len(values) > MAX_VISA_TYPES:
        raise ValidationFailedError("too many visa_types")
    for value in values:
        if not isinstance(value, str) or not _VISA_TYPE.match(value):
            raise ValidationFailedError(f"invalid visa type: {value!r}")
    return sorted(set(values))


def build_pipeline(
    *,
    index_name: str,
    query_vector: list[float],
    top_k: int,
    num_candidates: int,
    visa_types: list[str],
) -> list[dict[str, Any]]:
    """The approval filter is always applied server-side; callers cannot pass a filter."""
    if top_k < 1 or num_candidates < top_k or num_candidates > 10000:
        raise ValidationFailedError("invalid top_k/numCandidates")
    filter_doc: dict[str, Any] = {"is_active": True, "review_status": "approved"}
    if visa_types:
        filter_doc["visa_types"] = {"$in": visa_types}
    return [
        {
            "$vectorSearch": {
                "index": index_name,
                "path": "embedding",
                "queryVector": query_vector,
                "numCandidates": num_candidates,
                "limit": top_k,
                "filter": filter_doc,
            }
        },
        {"$project": {"embedding": 0}},
        {"$set": {"score": {"$meta": "vectorSearchScore"}}},
    ]


@dataclass(frozen=True)
class SearchHit:
    policy_document_id: int
    document_version: str
    chunk_index: int
    chunk_text: str
    title: str
    source_url: str
    source_org: str
    visa_types: list[str]
    score: float
    page_number: int | None = None
    section_title: str | None = None


def filter_current(
    hits: list[dict[str, Any]], gateway: PolicyDocumentGateway
) -> list[dict[str, Any]]:
    """Drop hits whose version is not the MySQL approved/current one.

    Guards against search index lag after approval changes. Any gateway failure is raised as
    :class:`SearchUnavailableError` so unverified candidates are never returned.
    """
    if not hits:
        return []
    ids = {int(hit["policy_document_id"]) for hit in hits}
    try:
        current = gateway.current_versions(ids)
    except ContractBlockedError as exc:
        raise SearchUnavailableError(str(exc)) from exc
    except Exception as exc:
        raise SearchUnavailableError("approval state check failed") from exc
    return [
        hit
        for hit in hits
        if current.get(int(hit["policy_document_id"])) == hit["document_version"]
        and hit.get("is_active") is True
        and hit.get("review_status") == "approved"
    ]


def to_hit(doc: dict[str, Any]) -> SearchHit:
    return SearchHit(
        policy_document_id=int(doc["policy_document_id"]),
        document_version=doc["document_version"],
        chunk_index=int(doc["chunk_index"]),
        chunk_text=doc["chunk_text"],
        title=doc["title"],
        source_url=doc["source_url"],
        source_org=doc["source_org"],
        visa_types=list(doc["visa_types"]),
        score=float(doc["score"]),
        page_number=doc.get("page_number"),
        section_title=doc.get("section_title"),
    )


class SearchService:
    def __init__(
        self,
        collection: Collection[dict[str, Any]],
        embedder: Embedder,
        gateway: PolicyDocumentGateway,
        *,
        index_name: str,
        num_candidates: int,
    ) -> None:
        self.collection = collection
        self.embedder = embedder
        self.gateway = gateway
        self.index_name = index_name
        self.num_candidates = num_candidates

    def search(self, query: str, top_k: int, visa_types: list[str] | None) -> list[SearchHit]:
        # English and Korean queries go through the same BGE-M3 model; no translation step.
        vector = self.embedder.embed_query(query)
        pipeline = build_pipeline(
            index_name=self.index_name,
            query_vector=vector,
            top_k=top_k,
            num_candidates=max(self.num_candidates, top_k),
            visa_types=validate_visa_types(visa_types),
        )
        raw = list(self.collection.aggregate(pipeline))
        return [to_hit(doc) for doc in filter_current(raw, self.gateway)]

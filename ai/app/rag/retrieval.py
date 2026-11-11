"""승인·활성 청크를 대상으로 한 벡터 검색. MySQL 버전 확인은 fail-closed로 한다."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from pymongo.collection import Collection

from app.integrations.policy_gateway import PolicyDocumentGateway
from app.rag.embeddings import Embedder
from app.rag.errors import ContractBlockedError, ValidationFailedError

_VISA_TYPE = re.compile(r"^[A-Z]-\d{1,2}(?:-\d{1,2})?$")
MAX_VISA_TYPES = 10
CANDIDATES_PER_RESULT = 20
SEOUL = ZoneInfo("Asia/Seoul")


class SearchUnavailableError(Exception):
    """승인 상태를 확인하지 못했다. 후보를 하나도 반환하면 안 된다."""


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
    embedding_model: str,
) -> list[dict[str, Any]]:
    """승인 필터는 항상 서버 측에서 적용한다. 호출자는 필터를 넘길 수 없다.

    ``embedding_model``은 ``$vectorSearch`` 바로 뒤에서 강제한다. 그래야 다른 모델의
    벡터(같은 1024차원, 다른 공간)가 순위에 섞이거나 청크를 중복시키지 않는다.
    확정된 인덱스 정의에 ``embedding_model`` 필터 필드가 없어서 후처리 필터로 둔다.
    앞으로 모델을 바꿀 때 이 필터 필드를 함께 추가한다(decisions.md 참고).
    """
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
        {"$match": {"embedding_model": embedding_model}},
        {"$project": {"embedding": 0}},
        {"$set": {"score": {"$meta": "vectorSearchScore"}}},
    ]


@dataclass(frozen=True)
class SearchHit:
    """검색 결과로 반환하는 청크 하나와 출처 메타데이터."""

    policy_document_id: int
    document_version: str
    chunk_index: int
    chunk_text: str
    title: str
    source_url: str
    source_org: str
    visa_types: list[str]
    score: float
    chunk_id: str = ""
    source_type: str = ""
    issued_on: date | None = None
    page_number: int | None = None
    section_title: str | None = None
    # 한국 달력 날짜(KST 자정을 UTC로 저장).
    effective_from: date | None = None
    effective_to: date | None = None


def _kst_date(value: datetime | None) -> date | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(SEOUL).date()


def filter_current(
    hits: list[dict[str, Any]], gateway: PolicyDocumentGateway
) -> list[dict[str, Any]]:
    """버전이 MySQL의 승인/현재 버전이 아닌 결과를 버린다.

    승인 상태가 바뀐 뒤 검색 인덱스 반영이 늦어지는 경우를 막는다. gateway 실패는 모두
    :class:`SearchUnavailableError`로 올려 검증되지 않은 후보가 반환되지 않게 한다.
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
        chunk_id=str(doc["_id"]),
        source_type=doc["source_type"],
        issued_on=_kst_date(doc.get("issued_at")),
        page_number=doc.get("page_number"),
        section_title=doc.get("section_title"),
        effective_from=_kst_date(doc.get("effective_from")),
        effective_to=_kst_date(doc.get("effective_to")),
    )


class SearchService:
    """질의를 임베딩해 승인·활성 청크를 벡터 검색하는 서비스."""

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
        # 영어·한국어 질의 모두 같은 BGE-M3 모델을 거친다. 번역 단계는 없다.
        vector = self.embedder.embed_query(query)
        pipeline = build_pipeline(
            index_name=self.index_name,
            query_vector=vector,
            top_k=top_k,
            # 설계 명세 v1.0: max(topK * 20, 100). 하한값은 settings에서 가져온다.
            num_candidates=max(self.num_candidates, top_k * CANDIDATES_PER_RESULT),
            visa_types=validate_visa_types(visa_types),
            embedding_model=self.embedder.model_name,
        )
        raw = list(self.collection.aggregate(pipeline))
        return [to_hit(doc) for doc in filter_current(raw, self.gateway)]

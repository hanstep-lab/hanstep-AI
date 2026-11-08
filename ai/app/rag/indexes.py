"""Atlas Vector Search index: compare, create (explicitly), wait for queryable, smoke test.

Never drops or rebuilds an index automatically.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pymongo.collection import Collection
from pymongo.operations import SearchIndexModel

from app.core.config import AI_ROOT

VECTOR_INDEX_DEFINITION_PATH = AI_ROOT / "scripts" / "atlas_vector_index.json"

IndexState = Literal["missing", "match", "drift"]


def load_definition(path: Path = VECTOR_INDEX_DEFINITION_PATH) -> dict[str, Any]:
    definition: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return definition


def _normalize_fields(definition: dict[str, Any]) -> list[dict[str, Any]]:
    fields = definition.get("fields", [])
    return sorted(
        ({k: v for k, v in field.items()} for field in fields),
        key=lambda f: (f.get("path", ""), f.get("type", "")),
    )


def definitions_equal(current: dict[str, Any], expected: dict[str, Any]) -> bool:
    return _normalize_fields(current) == _normalize_fields(expected)


@dataclass(frozen=True)
class VectorIndexStatus:
    state: IndexState
    name: str
    queryable: bool = False
    status: str = ""
    current_definition: dict[str, Any] | None = None


def inspect_vector_index(
    collection: Collection[dict[str, Any]], name: str, expected: dict[str, Any]
) -> VectorIndexStatus:
    for index in collection.list_search_indexes(name):
        current = index.get("latestDefinition") or index.get("definition") or {}
        state: IndexState = "match" if definitions_equal(current, expected) else "drift"
        if index.get("type") not in (None, "vectorSearch"):
            state = "drift"
        return VectorIndexStatus(
            state,
            name,
            bool(index.get("queryable")),
            str(index.get("status", "")),
            current,
        )
    return VectorIndexStatus("missing", name)


def create_vector_index(
    collection: Collection[dict[str, Any]], name: str, definition: dict[str, Any]
) -> str:
    model = SearchIndexModel(definition=definition, name=name, type="vectorSearch")
    return collection.create_search_index(model)


def wait_until_queryable(
    collection: Collection[dict[str, Any]],
    name: str,
    expected: dict[str, Any],
    *,
    timeout_s: float,
    poll_s: float = 3.0,
    sleep: Callable[[float], None] = time.sleep,
) -> VectorIndexStatus:
    deadline = time.monotonic() + timeout_s
    while True:
        status = inspect_vector_index(collection, name, expected)
        if status.state == "match" and status.queryable:
            return status
        if time.monotonic() >= deadline:
            return status
        sleep(poll_s)


def smoke_query(
    collection: Collection[dict[str, Any]], name: str, vector: list[float]
) -> list[dict[str, Any]]:
    """Run a real $vectorSearch with the mandatory approval filter."""
    pipeline: list[dict[str, Any]] = [
        {
            "$vectorSearch": {
                "index": name,
                "path": "embedding",
                "queryVector": vector,
                "numCandidates": 10,
                "limit": 1,
                "filter": {"is_active": True, "review_status": "approved"},
            }
        },
        {"$project": {"_id": 1}},
    ]
    return list(collection.aggregate(pipeline))

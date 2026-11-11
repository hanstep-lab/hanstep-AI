from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.integrations.policy_gateway import PolicyDocumentRecord
from app.rag.errors import ValidationFailedError
from app.rag.reconcile import MongoState, decide, load_snapshot

pytestmark = pytest.mark.unit


def _rec(status: Any, searchable: bool = False) -> PolicyDocumentRecord:
    return PolicyDocumentRecord(1, "v1", "a" * 64, status, searchable)


@pytest.mark.parametrize(
    ("record", "state", "verify_error", "expected"),
    [
        # 승인 + 검색 가능
        (_rec("APPROVED", True), MongoState(), None, "not_ingested"),
        (_rec("APPROVED", True), MongoState(3, 0, frozenset({"pending"})), None, "activate"),
        (_rec("APPROVED", True), MongoState(3, 3, frozenset({"approved"})), None, "ok"),
        (_rec("APPROVED", True), MongoState(3, 3, frozenset({"approved"}), 2), None, "deactivate"),
        (
            _rec("APPROVED", True),
            MongoState(3, 0, frozenset({"pending"})),
            "hash differs",
            "blocked",
        ),
        # 검색 불가 상태
        (_rec("RETIRED"), MongoState(3, 3, frozenset({"approved"})), None, "deactivate"),
        (_rec("DRAFT"), MongoState(3, 0, frozenset({"pending"})), None, "ok"),
        (_rec("APPROVED", False), MongoState(3, 0, frozenset({"pending"})), None, "update"),
        (_rec("NEEDS_REVIEW"), MongoState(3, 0, frozenset({"approved"})), None, "update"),
        (_rec("DRAFT"), MongoState(0, 0, frozenset(), 1), None, "deactivate"),
        (_rec("DRAFT"), MongoState(), None, "ok"),
    ],
)
def test_decide(
    record: PolicyDocumentRecord, state: MongoState, verify_error: str | None, expected: str
) -> None:
    assert decide(record, state, verify_error)[0] == expected


def _write(tmp_path: Path, docs: list[dict[str, Any]]) -> Path:
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({"documents": docs}), encoding="utf-8")
    return path


DOC = {
    "policyDocumentId": 12,
    "policyVersion": "E74-2026-v1",
    "contentHash": "a" * 64,
    "reviewStatus": "APPROVED",
    "searchable": True,
    "chunkCount": 41,
}


def test_load_snapshot_parses_camel_case(tmp_path: Path) -> None:
    snapshot = load_snapshot(_write(tmp_path, [DOC]))
    record = snapshot.documents[0].record()
    assert (record.policy_document_id, record.review_status, record.searchable) == (
        12,
        "APPROVED",
        True,
    )
    assert snapshot.documents[0].chunk_count == 41


@pytest.mark.parametrize(
    "bad",
    [
        {**DOC, "reviewStatus": "approved"},  # MySQL ENUM 값만 허용
        {**DOC, "contentHash": "xyz"},
        {**DOC, "extra": 1},
        {k: v for k, v in DOC.items() if k != "searchable"},
    ],
)
def test_load_snapshot_rejects_invalid_rows(tmp_path: Path, bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        load_snapshot(_write(tmp_path, [bad]))


def test_load_snapshot_rejects_duplicate_ids(tmp_path: Path) -> None:
    with pytest.raises(ValidationFailedError):
        load_snapshot(_write(tmp_path, [DOC, {**DOC, "policyVersion": "v2"}]))

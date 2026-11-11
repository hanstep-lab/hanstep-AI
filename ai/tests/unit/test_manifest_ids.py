from __future__ import annotations

from typing import Any

import pytest

from app.rag.manifest_ids import parse_export, plan_fill

pytestmark = pytest.mark.unit

A, B = "a" * 64, "b" * 64


def _manifest(**first: Any) -> dict[str, Any]:
    return {
        "sources": [
            {
                "path": "visa/a.pdf",
                "sha256": A,
                "policy_document_id": None,
                "document_version": None,
                **first,
            },
            {
                "path": "visa/b.pdf",
                "sha256": B,
                "policy_document_id": None,
                "document_version": None,
            },
        ]
    }


def test_parse_mysql_tsv_with_header() -> None:
    rows = parse_export(f"id\tpolicy_version\tcontent_hash\n12\tE74-v1\t{A}\n13\tE74-v2\t{B}\n")
    assert rows == [
        {"id": 12, "policy_version": "E74-v1", "content_hash": A},
        {"id": 13, "policy_version": "E74-v2", "content_hash": B},
    ]


def test_parse_json_camel_case() -> None:
    rows = parse_export('[{"id": 12, "policyVersion": "E74-v1", "contentHash": "' + A + '"}]')
    assert rows[0]["id"] == 12


@pytest.mark.parametrize(
    "text",
    [
        f"x\tv\t{A}",
        f"0\tv\t{A}",
        "12\tv\tnot-a-hash",
        f"12\t\t{A}",
        f"12\tv1\t{A}\n13\tv2\t{A}",
        "12\tonly-two",
    ],
)
def test_parse_rejects_bad_rows(text: str) -> None:
    with pytest.raises(ValueError):
        parse_export(text)


def test_plan_matches_by_hash_only() -> None:
    plan = plan_fill(_manifest(), parse_export(f"13\tE74-v2\t{B}\n12\tE74-v1\t{A}"))
    assert {c["path"]: c["policy_document_id"] for c in plan["changes"]} == {
        "visa/a.pdf": 12,
        "visa/b.pdf": 13,
    }
    assert plan["conflicts"] == [] and plan["unmatched_export_rows"] == []


def test_plan_never_overwrites_existing_ids() -> None:
    plan = plan_fill(
        _manifest(policy_document_id=99, document_version="old"), parse_export(f"12\tE74-v1\t{A}")
    )
    assert plan["changes"] == []
    assert plan["conflicts"][0]["current"] == (99, "old")


def test_plan_reports_rows_for_unknown_files() -> None:
    plan = plan_fill(_manifest(), parse_export("14\tv\t" + "c" * 64))
    assert plan["unmatched_export_rows"][0]["id"] == 14

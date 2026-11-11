"""MySQL ``policy_documents`` export에서 콘텐츠 해시로 매칭해 매니페스트 ID를 채운다."""

from __future__ import annotations

import json
import re
from typing import Any

_SHA = re.compile(r"^[0-9a-f]{64}$")


def parse_export(text: str) -> list[dict[str, Any]]:
    """{id, policy_version, content_hash} 행 목록을 반환한다. 잘못된 입력이면 ValueError를 낸다."""
    stripped = text.strip()
    rows: list[dict[str, Any]] = []
    if stripped.startswith(("[", "{")):
        data = json.loads(stripped)
        items = data.get("documents", data) if isinstance(data, dict) else data
        for item in items:
            rows.append(
                {
                    "id": item.get("id", item.get("policyDocumentId")),
                    "policy_version": item.get("policyVersion", item.get("policy_version")),
                    "content_hash": item.get("contentHash", item.get("content_hash")),
                }
            )
    else:
        for line in stripped.splitlines():
            parts = [p.strip() for p in re.split(r"\t|,", line) if p.strip()]
            if not parts or parts[0].lower() == "id":
                continue
            if len(parts) != 3:
                raise ValueError(f"expected 3 columns (id, policy_version, content_hash): {line!r}")
            rows.append({"id": parts[0], "policy_version": parts[1], "content_hash": parts[2]})
    for row in rows:
        if not str(row["id"]).isdigit() or int(row["id"]) < 1:
            raise ValueError(f"invalid id: {row['id']!r}")
        row["id"] = int(row["id"])
        if not row["policy_version"]:
            raise ValueError(f"missing policy_version for id {row['id']}")
        if not _SHA.match(str(row["content_hash"])):
            raise ValueError(f"invalid content_hash for id {row['id']}")
    hashes = [r["content_hash"] for r in rows]
    if len(set(hashes)) != len(hashes):
        raise ValueError("export repeats a content_hash")
    return rows


def plan_fill(manifest: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_hash = {r["content_hash"]: r for r in rows}
    changes, conflicts = [], []
    matched: set[str] = set()
    for entry in manifest["sources"]:
        row = by_hash.get(entry["sha256"])
        if row is None:
            continue
        matched.add(row["content_hash"])
        current = (entry.get("policy_document_id"), entry.get("document_version"))
        target = (row["id"], row["policy_version"])
        if current == target:
            continue
        if current != (None, None):
            # 이 파일에 이미 기록된 ID는 절대 조용히 덮어쓰지 않는다.
            conflicts.append({"path": entry["path"], "current": current, "export": target})
            continue
        changes.append(
            {
                "path": entry["path"],
                "policy_document_id": row["id"],
                "document_version": row["policy_version"],
            }
        )
    unmatched = [r for r in rows if r["content_hash"] not in matched]
    return {"changes": changes, "conflicts": conflicts, "unmatched_export_rows": unmatched}

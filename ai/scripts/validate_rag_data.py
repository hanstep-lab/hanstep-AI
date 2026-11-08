"""Validate stored chunks against the manifest and the confirmed contract (read-only).

Checks per manifest entry: chunk count/contiguity, ChunkRecord validation (types, 1024-dim
finite vectors, is_active => approved), source hash, and global duplicates/active conflicts.

    uv run python scripts/validate_rag_data.py --manifest specs/rag/sources.json \
        --output artifacts/rag/validation.json
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401
from pydantic import ValidationError

from app.core.config import get_settings
from app.rag.cli import EXIT_BLOCKED, EXIT_FAILED, EXIT_OK, print_json, write_json
from app.rag.manifest import load_manifest
from app.rag.repository import ChunkRepository
from app.rag.schemas import ChunkRecord


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("artifacts/rag/validation.json"))
    args = parser.parse_args()

    settings = get_settings()
    if not settings.mongo_configured():
        print("BLOCKED: MONGO_URI is not configured")
        return EXIT_BLOCKED
    from pymongo import MongoClient

    manifest = load_manifest(args.manifest)
    client: MongoClient[dict[str, Any]] = MongoClient(
        settings.mongo_uri.get_secret_value(), serverSelectionTimeoutMS=5000, tz_aware=True
    )
    problems: list[str] = []
    entries: list[dict[str, object]] = []
    try:
        repo = ChunkRepository(client[settings.mongo_database], settings.mongo_collection)
        invalid = 0
        for doc in repo.collection.find({}):
            try:
                ChunkRecord.from_bson(doc)
            except ValidationError as exc:
                invalid += 1
                if invalid <= 5:
                    problems.append(f"invalid record {doc.get('_id')}: {exc.errors()[:1]}")
        duplicates = repo.find_duplicates()
        if duplicates:
            problems.append(f"duplicate keys: {duplicates[:5]}")
        multi_active = list(
            repo.collection.aggregate(
                [
                    {"$match": {"is_active": True}},
                    {
                        "$group": {
                            "_id": "$policy_document_id",
                            "v": {"$addToSet": "$document_version"},
                        }
                    },
                    {"$match": {"v.1": {"$exists": True}}},
                ]
            )
        )
        if multi_active:
            problems.append(f"documents with several active versions: {multi_active[:5]}")

        for entry in manifest.sources:
            if entry.policy_document_id is None or entry.document_version is None:
                entries.append({"path": entry.path, "status": "not_registered"})
                continue
            chunks = repo.load_version(
                entry.policy_document_id, entry.document_version, settings.embedding_model
            )
            indexes = [c.chunk_index for c in chunks]
            status = "ok"
            if not chunks:
                status = "missing"
            elif indexes != list(range(len(chunks))):
                status = "gap"
            elif any(c.source_sha256 != entry.sha256 for c in chunks):
                status = "hash_mismatch"
            if status not in ("ok", "missing"):
                problems.append(f"{entry.path}: {status}")
            entries.append(
                {
                    "path": entry.path,
                    "status": status,
                    "chunks": len(chunks),
                    "review_status": sorted({c.review_status for c in chunks}),
                    "active": sum(c.is_active for c in chunks),
                }
            )
        report = {
            "collection": f"{settings.mongo_database}.{settings.mongo_collection}",
            "invalid_records": invalid,
            "problems": problems,
            "entries": entries,
        }
    finally:
        client.close()
    write_json(args.output, report)
    print_json(report)
    return EXIT_FAILED if problems else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

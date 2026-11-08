"""Create/verify the chunk collection validator, unique index and Atlas vector index.

--dry-run (default): print the definitions; touch nothing.
--check:             connect and report missing/match/drift without writing.
--apply:             create what is missing, wait until queryable, run a real search smoke
                     test. Drift is reported; nothing is ever dropped or rebuilt.

    uv run python scripts/create_atlas_indexes.py --dry-run
    uv run python scripts/create_atlas_indexes.py --apply --wait-timeout 180
"""

from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from app.core.config import configure_logging, get_settings
from app.rag.cli import EXIT_BLOCKED, EXIT_FAILED, EXIT_OK, print_json
from app.rag.errors import ValidationFailedError
from app.rag.indexes import (
    create_vector_index,
    inspect_vector_index,
    load_definition,
    smoke_query,
    wait_until_queryable,
)
from app.rag.repository import ChunkRepository
from app.rag.schemas import UNIQUE_INDEX_KEYS, UNIQUE_INDEX_NAME, json_schema_validator


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--wait-timeout", type=float, default=180.0)
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level)
    definition = load_definition()
    target = {
        "database": settings.mongo_database,
        "collection": settings.mongo_collection,
        "vector_index": settings.mongo_vector_index,
    }
    if not (args.check or args.apply):
        print_json(
            {
                **target,
                "validator": json_schema_validator(),
                "unique_index": {"name": UNIQUE_INDEX_NAME, "keys": UNIQUE_INDEX_KEYS},
                "vector_index_definition": definition,
            }
        )
        return EXIT_OK
    if not settings.mongo_configured():
        print("BLOCKED: MONGO_URI is not configured")
        return EXIT_BLOCKED

    from pymongo import MongoClient

    client: MongoClient[dict[str, object]] = MongoClient(
        settings.mongo_uri.get_secret_value(), serverSelectionTimeoutMS=5000, tz_aware=True
    )
    try:
        repo = ChunkRepository(client[settings.mongo_database], settings.mongo_collection)
        report: dict[str, object] = dict(target)
        if args.apply:
            report["collection_validator"] = repo.ensure_collection()
            try:
                report["unique_index"] = repo.ensure_unique_index()
            except ValidationFailedError as exc:
                report["unique_index"] = f"blocked: {exc}"
        else:
            report["collection_validator"] = repo.validator_status()[0]
            report["unique_index"] = repo.unique_index_status()

        status = inspect_vector_index(repo.collection, settings.mongo_vector_index, definition)
        if status.state == "missing" and args.apply:
            create_vector_index(repo.collection, settings.mongo_vector_index, definition)
            report["vector_index_action"] = "created"
        if args.apply and status.state != "drift":
            status = wait_until_queryable(
                repo.collection,
                settings.mongo_vector_index,
                definition,
                timeout_s=args.wait_timeout,
            )
        report["vector_index"] = {
            "state": status.state,
            "queryable": status.queryable,
            "status": status.status,
        }
        if status.state == "drift":
            report["vector_index_drift"] = {
                "current": status.current_definition,
                "expected": definition,
            }

        ready = status.state == "match" and status.queryable
        if args.apply and ready:
            probe = [0.0] * 1023 + [1.0]
            smoke_query(repo.collection, settings.mongo_vector_index, probe)
            report["smoke_test"] = "passed"
        print_json(report)
        if str(report["unique_index"]).startswith("blocked") or str(
            report["collection_validator"]
        ).startswith("drift"):
            return EXIT_FAILED
        if not ready:
            return EXIT_FAILED if args.apply else EXIT_BLOCKED
        return EXIT_OK
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())

"""Compare MongoDB chunk state with a MySQL policy_documents snapshot and optionally fix drift.

Default is read-only (plan only). --apply fixes each drifting row through sync_policy_state.
--complete-snapshot means the file lists every policy_documents row, so documents active in
MongoDB but absent from the snapshot are deactivated as orphans.

Snapshot JSON (until a Spring listing API exists):
    {"documents": [{"policyDocumentId": 12, "policyVersion": "E74-2026-v1",
                    "contentHash": "<sha256>", "reviewStatus": "APPROVED",
                    "searchable": true, "chunkCount": 41}]}

    uv run python scripts/reconcile_policy_state.py --snapshot snapshot.json
    uv run python scripts/reconcile_policy_state.py --snapshot snapshot.json --apply
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401
from pydantic import ValidationError

from app.core.config import configure_logging, get_settings
from app.rag.cli import (
    EXIT_BLOCKED,
    EXIT_FAILED,
    EXIT_OK,
    new_run_id,
    print_json,
    write_json,
)
from app.rag.errors import ValidationFailedError
from app.rag.reconcile import apply_plan, build_plan, load_snapshot, summarize
from app.rag.repository import ChunkRepository


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--complete-snapshot", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        snapshot = load_snapshot(args.snapshot)
    except (ValidationError, ValidationFailedError, ValueError) as exc:
        print(f"invalid snapshot: {exc}")
        return EXIT_FAILED
    if not settings.mongo_configured():
        print("BLOCKED: MONGO_URI is not configured")
        return EXIT_BLOCKED

    from pymongo import MongoClient

    client: MongoClient[dict[str, Any]] = MongoClient(
        settings.mongo_uri.get_secret_value(), serverSelectionTimeoutMS=5000, tz_aware=True
    )
    run_id = new_run_id()
    try:
        repo = ChunkRepository(client[settings.mongo_database], settings.mongo_collection)
        plan = build_plan(
            repo,
            snapshot,
            embedding_model=settings.embedding_model,
            complete=args.complete_snapshot,
        )
        if args.apply:
            apply_plan(client, repo, snapshot, plan, embedding_model=settings.embedding_model)
    finally:
        client.close()

    report = {
        "run_id": run_id,
        "mode": "apply" if args.apply else "plan",
        "snapshot": args.snapshot.as_posix(),
        "documents": len(snapshot.documents),
        **summarize(plan),
    }
    output: Path = args.output or settings.rag_artifacts_dir / "reconcile" / f"{run_id}.json"
    write_json(output, report)
    print_json(report)
    print(f"report: {output}")

    blocked = [i for i in plan.items if i.action == "blocked" or i.result.startswith("error")]
    if blocked:
        return EXIT_FAILED
    # In plan mode, remaining drift is a non-zero exit so a scheduled run can alert.
    if not args.apply and plan.changes:
        return EXIT_FAILED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

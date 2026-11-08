"""Ingest manually supplied documents listed in the manifest.

--dry-run: preflight, extract and chunk only. No MongoDB/MySQL/R2 writes, no model download,
           no network. Reports what --apply would block on.
--apply:   requires every contract (MySQL gateway, R2, MongoDB, model). New chunks are written
           as pending/inactive; this command never approves or activates anything.

    uv run python scripts/ingest_rag_documents.py --manifest specs/rag/sources.json --dry-run
    uv run python scripts/ingest_rag_documents.py --manifest specs/rag/sources.json \
        --domain visa --apply [--only visa/selection/e74_selection_20260601.pdf]
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401

from app.core.config import configure_logging, get_settings
from app.integrations.policy_gateway import UnconfiguredPolicyGateway
from app.integrations.r2 import BUCKET_SETTINGS, UnconfiguredObjectStore, build_object_stores
from app.rag.chunking import approx_token_count
from app.rag.cli import (
    EXIT_BLOCKED,
    EXIT_FAILED,
    EXIT_OK,
    new_run_id,
    print_json,
    write_json,
)
from app.rag.embeddings import BgeM3Embedder, local_tokenizer_counter
from app.rag.ingestion import IngestionDeps, process_entry
from app.rag.manifest import load_manifest
from app.rag.repository import ChunkRepository

SUCCESS = {"planned", "loaded", "skipped"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--domain", choices=["visa", "finance"])
    parser.add_argument("--only", action="append", default=[], help="relative path subset")
    parser.add_argument("--run-id", help="reuse a run id when resuming")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level)
    run_id = args.run_id or new_run_id()
    manifest = load_manifest(args.manifest)
    root = manifest.resolve_root(args.manifest)
    entries = [
        e
        for e in manifest.sources
        if (not args.domain or e.domain == args.domain) and (not args.only or e.path in args.only)
    ]
    unknown = set(args.only) - {e.path for e in manifest.sources}
    if unknown:
        print(f"--only paths not in manifest: {sorted(unknown)}")
        return EXIT_FAILED

    repository = None
    embedder = None
    mongo_client: Any = None
    if args.apply:
        if not settings.mongo_configured():
            print("BLOCKED: MONGO_URI is not configured")
            return EXIT_BLOCKED
        from pymongo import MongoClient

        mongo_client = MongoClient(
            settings.mongo_uri.get_secret_value(), serverSelectionTimeoutMS=5000, tz_aware=True
        )
        repository = ChunkRepository(
            mongo_client[settings.mongo_database], settings.mongo_collection
        )
        if repository.unique_index_status() != "match":
            print("BLOCKED: unique index missing; run scripts/create_atlas_indexes.py --apply")
            return EXIT_BLOCKED
        embedder = BgeM3Embedder(
            settings.embedding_model,
            revision=settings.embedding_model_revision,
            device=settings.embedding_device,
            batch_size=settings.embedding_batch_size,
        )
        counter = embedder.count_tokens
        tokenizer_id = (
            f"{settings.embedding_model}@{settings.embedding_model_revision or 'default'}"
        )
    else:
        local = local_tokenizer_counter(settings.embedding_model, settings.embedding_model_revision)
        counter = local or approx_token_count
        tokenizer_id = f"{settings.embedding_model}@local-cache" if local else "approx-chars/1.6"

    deps = IngestionDeps(
        repository=repository,
        gateway=UnconfiguredPolicyGateway(),
        object_stores=build_object_stores(settings)
        if args.apply
        else {d: UnconfiguredObjectStore(b) for d, b in BUCKET_SETTINGS.items()},
        embedder=embedder,
        token_counter=counter,
        tokenizer_id=tokenizer_id,
        r2_prefix=settings.r2_policy_prefix,
        embedding_model=settings.embedding_model,
    )
    try:
        results = [process_entry(e, root, deps, apply=args.apply) for e in entries]
    finally:
        if mongo_client is not None:
            mongo_client.close()

    counts = Counter(r.status for r in results)
    report = {
        "run_id": run_id,
        "mode": "apply" if args.apply else "dry-run",
        "manifest": args.manifest.as_posix(),
        "tokenizer": tokenizer_id,
        "chunker": deps.chunker.__dict__,
        "embedder": embedder.info().as_dict() if embedder else None,
        "counts": dict(counts),
        "results": results,
        "rerun": f"--run-id {run_id} "
        + " ".join(f"--only {r.path}" for r in results if r.status not in SUCCESS),
    }
    output_dir: Path = args.output_dir or settings.rag_artifacts_dir / "runs"
    write_json(output_dir / f"{run_id}.json", report)
    print_json(
        {
            "run_id": run_id,
            "mode": report["mode"],
            "counts": report["counts"],
            "files": [
                {"path": r.path, "status": r.status, "chunks": r.chunks, "reasons": r.reasons}
                for r in results
            ],
        }
    )
    print(f"report: {output_dir / f'{run_id}.json'}")
    if any(r.status == "failed" for r in results):
        return EXIT_FAILED
    if args.apply and any(r.status not in SUCCESS for r in results):
        return EXIT_BLOCKED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

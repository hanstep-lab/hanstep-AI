"""Collect official documents through provider APIs (first adapter: 법제처 law API).

--dry-run:    validate the config and parse local fixtures; no network, no writes.
--fetch-only: call the provider read APIs only (needs LAW_API_OC); store responses under
              artifacts/ for inspection; no MongoDB/MySQL/R2 writes.
--apply:      fetch -> MySQL registration -> R2 -> chunk/embed -> MongoDB pending -> verify.
              Exits BLOCKED until the policy_documents/ai_jobs contracts are confirmed.

    uv run python scripts/sync_rag_api.py --config specs/rag/api_sources.json --dry-run
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401

from app.core.config import AI_ROOT, configure_logging, get_settings, is_placeholder
from app.integrations.law_api import (
    LawApiClient,
    LawApiError,
    law_extraction_report,
    parse_law,
    parse_search,
    select_exact,
)
from app.integrations.policy_gateway import MISSING_JOB_CONTRACT, MISSING_POLICY_CONTRACT
from app.rag.chunking import approx_token_count, chunk_blocks
from app.rag.cli import EXIT_BLOCKED, EXIT_FAILED, EXIT_OK, new_run_id, print_json, write_json
from app.rag.errors import ContractBlockedError, ValidationFailedError


def load_config(path: Path) -> dict[str, Any]:
    config: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    providers = config.get("providers")
    if not isinstance(providers, list) or not providers:
        raise ValidationFailedError("config.providers must be a non-empty list")
    for provider in providers:
        if provider.get("provider") != "law_go_kr":
            raise ValidationFailedError(f"unsupported provider: {provider.get('provider')}")
        if not provider.get("laws"):
            raise ValidationFailedError("law_go_kr.laws must list exact law names")
    return config


def dry_run(config: dict[str, Any]) -> dict[str, Any]:
    results = []
    for provider in config["providers"]:
        fixtures = AI_ROOT / provider["fixtures_dir"]
        _, laws = parse_search((fixtures / "lawSearch.xml").read_bytes())
        for name in provider["laws"]:
            entry: dict[str, Any] = {"law": name}
            try:
                summary = select_exact(laws, name)
                body = fixtures / f"lawService_{summary.mst}.xml"
                doc = parse_law(body.read_bytes())
                report = law_extraction_report(doc)
                chunks = chunk_blocks(report.blocks, approx_token_count)
                entry.update(
                    status="parsed_fixture",
                    mst=summary.mst,
                    articles=doc.article_count,
                    addenda=doc.addenda_count,
                    annexes=doc.annex_count,
                    chunks=len(chunks),
                    raw_sha256=doc.raw_sha256,
                )
            except (ValidationFailedError, FileNotFoundError, LawApiError) as exc:
                entry.update(status="fixture_missing", reason=str(exc))
            results.append(entry)
    return {"mode": "dry-run", "source": "local fixtures (not real API responses)", "laws": results}


def fetch_only(config: dict[str, Any], run_id: str) -> dict[str, Any]:
    settings = get_settings()
    oc = settings.law_api_oc.get_secret_value()
    if is_placeholder(oc):
        raise ContractBlockedError("LAW_API_OC is not set in ai/.env")
    out_dir = settings.rag_artifacts_dir / "api" / run_id
    client = LawApiClient(base_url=settings.law_api_base_url, oc=oc)
    results = []
    try:
        for provider in config["providers"]:
            for doc in client.collect_targets(tuple(provider["laws"])):
                path = out_dir / f"{doc.law_id}_{doc.raw_sha256[:12]}.xml"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(doc.raw_xml)
                results.append(
                    {
                        "law": doc.name,
                        "law_id": doc.law_id,
                        "promulgation_date": doc.promulgation_date,
                        "effective_date": doc.effective_date,
                        "articles": doc.article_count,
                        "addenda": doc.addenda_count,
                        "annexes": doc.annex_count,
                        "annexes_skipped": doc.skipped_annexes,
                        "raw_sha256": doc.raw_sha256,
                        "content_sha256": doc.content_sha256(),
                        "saved": path.as_posix(),
                    }
                )
    finally:
        client.close()
    return {"mode": "fetch-only", "writes": "local artifacts only", "laws": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--fetch-only", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level)
    run_id = new_run_id()
    try:
        config = load_config(args.config)
        if args.dry_run:
            report = dry_run(config)
        elif args.fetch_only:
            report = fetch_only(config, run_id)
        else:
            # Law documents need a policy_document_id/document_version issued by MySQL and a
            # job row; neither contract exists, so stop before calling the provider.
            raise ContractBlockedError(f"{MISSING_POLICY_CONTRACT}; {MISSING_JOB_CONTRACT}")
    except ContractBlockedError as exc:
        print_json({"run_id": run_id, "result": "BLOCKED", "reason": str(exc)})
        return EXIT_BLOCKED
    except (ValidationFailedError, LawApiError) as exc:
        print_json({"run_id": run_id, "result": "FAILED", "reason": str(exc)})
        return EXIT_FAILED
    report["run_id"] = run_id
    write_json(args.output or settings.rag_artifacts_dir / "api" / f"{run_id}.json", report)
    print_json(report)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

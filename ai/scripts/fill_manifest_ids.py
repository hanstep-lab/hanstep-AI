"""MySQL export를 읽어 manifest에 policy_document_id / document_version을 채운다.

입력은 아래 쿼리의 결과다.
    SELECT id, policy_version, content_hash FROM policy_documents WHERE content_hash IN (...);
형식은 TSV(mysql 클라이언트 기본, 헤더는 선택)
또는 JSON [{"id", "policyVersion", "contentHash"}]이다.
행은 content_hash == manifest sha256으로 맞춘다. 파일 이름으로는 아무것도 추측하지 않는다.

기본은 바뀔 내용만 출력한다. --write를 주면 manifest 파일을 고친다.

    uv run python scripts/fill_manifest_ids.py --manifest specs/rag/sources.json \
        --export ids.tsv [--write]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import _bootstrap  # noqa: F401

from app.rag.cli import EXIT_FAILED, EXIT_OK, print_json
from app.rag.manifest_ids import parse_export, plan_fill


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--export", type=Path, required=True)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    try:
        rows = parse_export(args.export.read_text(encoding="utf-8"))
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"invalid export: {exc}")
        return EXIT_FAILED
    plan = plan_fill(manifest, rows)
    print_json({"mode": "write" if args.write else "preview", **plan})
    if plan["conflicts"] or plan["unmatched_export_rows"]:
        print("not writing: resolve conflicts / unmatched rows first")
        return EXIT_FAILED
    if args.write and plan["changes"]:
        by_path = {c["path"]: c for c in plan["changes"]}
        for entry in manifest["sources"]:
            change = by_path.get(entry["path"])
            if change:
                entry["policy_document_id"] = change["policy_document_id"]
                entry["document_version"] = change["document_version"]
        args.manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"updated {len(plan['changes'])} entries in {args.manifest}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

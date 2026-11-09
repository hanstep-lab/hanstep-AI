"""Inventory the manual source folder: every file is found, excluded or failed, with reasons.

Offline only: no database, R2, network or model download.

    uv run python scripts/inspect_rag_sources.py --root ../data/rag_documents \
        --output artifacts/rag/inventory.json
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import _bootstrap  # noqa: F401

from app.rag.cli import EXIT_FAILED, EXIT_OK, print_json, write_json
from app.rag.extractors import extract
from app.rag.sources import discover, resolve_inside


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("../data/rag_documents"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/rag/inventory.json"))
    args = parser.parse_args()

    root: Path = args.root
    if not root.is_dir():
        print(f"data root not found: {root}")
        return EXIT_FAILED
    files = []
    for item in discover(root):
        entry: dict[str, object] = {
            "path": item.relative_path,
            "status": item.status,
            "reason": item.reason,
            "kind": item.detected_kind,
            "size_bytes": item.size_bytes,
            "sha256": item.sha256,
        }
        if item.status == "found":
            data = resolve_inside(root, item.relative_path).read_bytes()
            entry["extraction"] = extract(item.detected_kind, data).summary()
        files.append(entry)

    leaf_dirs = sorted(
        {
            p.parent.relative_to(root.resolve()).as_posix()
            for p in root.resolve().rglob("*")
            if p.is_file()
        }
    )
    summary = {
        "root": root.resolve().as_posix(),
        "files": len(files),
        "documents": sum(1 for f in files if f["status"] == "found"),
        "by_status": dict(Counter(str(f["status"]) for f in files)),
        "by_kind": dict(Counter(str(f["kind"]) for f in files if f["status"] == "found")),
        "by_extraction": dict(
            Counter(
                str(f["extraction"]["status"])  # type: ignore[index]
                for f in files
                if "extraction" in f
            )
        ),
        "leaf_folders": leaf_dirs,
    }
    write_json(args.output, {"summary": summary, "files": files})
    print_json(summary)
    print(f"wrote {args.output}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

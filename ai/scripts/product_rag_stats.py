"""금융상품 검색 실험 보고서들의 비교 통계(검색·임베딩 없이 보고서 JSON만 읽는다).

    uv run python scripts/product_rag_stats.py \
        --report v1-fixed=artifacts/rag/product-eval-1-2-3-<시각>.json \
        --report v1-holdout=artifacts/rag/product-eval-1-2-3-<시각>.json \
        --report v2=artifacts/rag/product-eval-1-2-3-<시각>.json

언어가 있는 보고서는 언어별(예: v2-ko, v2-en)과 전체로 나눠 계산한다.
결과: artifacts/rag/product-stats-{시각}.json 과 같은 이름의 .md
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import _bootstrap

from app.rag.cli import EXIT_FAILED, EXIT_OK, write_json
from app.rag.experiments.product_stats import (
    BOOTSTRAP_ROUNDS,
    BOOTSTRAP_SEED,
    build_statistics,
    render_statistics,
)

DEFAULT_OUTPUT_DIR = _bootstrap.AI_ROOT / "artifacts" / "rag"


def parse_report(value: str) -> tuple[str, Path]:
    name, sep, path = value.partition("=")
    if not sep or not name or not path:
        raise argparse.ArgumentTypeError("use NAME=PATH")
    return name, Path(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", type=parse_report, action="append", required=True)
    parser.add_argument("--decision-set", default="v2-ko")
    parser.add_argument("--rounds", type=int, default=BOOTSTRAP_ROUNDS)
    parser.add_argument("--seed", type=int, default=BOOTSTRAP_SEED)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    reports: dict[str, Any] = {}
    for name, path in args.report:
        if name in reports:
            print(f"FAILED: duplicate report name {name}")
            return EXIT_FAILED
        reports[name] = json.loads(path.read_text(encoding="utf-8"))
    stats = build_statistics(
        reports, decision_set=args.decision_set, rounds=args.rounds, seed=args.seed
    )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = args.output_dir / f"product-stats-{stamp}.json"
    write_json(json_path, stats)
    md_path = json_path.with_suffix(".md")
    md_path.write_text(render_statistics(stats), encoding="utf-8")
    print(f"wrote {json_path}\nwrote {md_path}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

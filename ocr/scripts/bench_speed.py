"""OCR 엔진 속도 측정(#110). 프로세스 하나에서 모델을 한 번 올리고, 이미지마다 워밍업 뒤 N회 잰다.

- 설정은 엔진과 같은 환경변수(OCR_DET_MODEL, OCR_MAX_SIDE)로 바꾼다. 조합마다 새 프로세스로 돌려야
  모델 로드 시간과 메모리가 섞이지 않는다.
- 시간은 Worker의 ocr_ms와 같은 구간이다(엔진 recognize 한 번: 이미지 읽기·리사이즈·추론·필드 추출).
- 결과는 JSON 한 줄씩(--out). 인식 글자(lines)도 남기므로 합성 이미지에만 쓴다. 실제 등록증 금지.

사용(Linux 컨테이너, ocr/ 에서):
    python scripts/bench_speed.py --label sdet-none --out /results/sdet-none.jsonl \
        /bench/sample_1.jpg /bench/sample_2.jpg /bench/sample_3.jpg
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import resource
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import engine  # noqa: E402
from pipeline.extract import extract_fields  # noqa: E402


def _max_rss_mb() -> float:
    """이 프로세스의 최대 RSS(MB). Linux ru_maxrss는 KB다."""
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


def _run_once(image: str) -> dict[str, object]:
    """recognize와 같은 구간을 재고, 채점용 인식 글자와 이미지 크기를 돌려준다."""
    started = time.perf_counter()
    analyze = getattr(engine, "analyze", None)
    if analyze is None:  # #110 전 엔진: 파일 경로를 그대로 PaddleOCR에 넘긴다
        lines = engine.read_lines(image)
        image_info: dict[str, object] = {}
    else:
        analysis = analyze(image)
        lines = analysis.lines
        image_info = analysis.timings()
    fields = extract_fields(lines)
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    return {
        "ocrMs": elapsed_ms,
        "imageInfo": image_info,
        "fields": {f["name"]: f["value"] for f in fields},
        "lines": [text for text, _ in lines],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("images", nargs="+")
    parser.add_argument("--label", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=1)
    # 측정 전용(엔진 설정은 바꾸지 않는다): PaddleOCR 스레드 수·mkldnn. 비우면 PaddleOCR 기본값.
    parser.add_argument("--cpu-threads", type=int, default=None)
    parser.add_argument("--mkldnn", choices=("on", "off"), default=None)
    args = parser.parse_args()

    extra: dict[str, object] = {}
    if args.cpu_threads is not None:
        extra["cpu_threads"] = args.cpu_threads
    if args.mkldnn is not None:
        extra["enable_mkldnn"] = args.mkldnn == "on"
    if extra:
        engine.PaddleOCR = functools.partial(engine.PaddleOCR, **extra)

    rss_before = _max_rss_mb()
    started = time.perf_counter()
    engine.load()
    load_ms = round((time.perf_counter() - started) * 1000)
    header = {
        "type": "run",
        "label": args.label,
        "modelVersion": engine.MODEL_VERSION,
        "env": {k: os.environ.get(k) for k in ("OCR_DET_MODEL", "OCR_MAX_SIDE")},
        "paddleArgs": extra,
        "cpuCount": os.cpu_count(),
        "loadMs": load_ms,
        "rssBeforeLoadMb": rss_before,
        "rssAfterLoadMb": _max_rss_mb(),
        "repeats": args.repeats,
        "warmup": args.warmup,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as out:
        out.write(json.dumps(header, ensure_ascii=False) + "\n")
        for image in args.images:
            for i in range(args.warmup + args.repeats):
                row = {"image": Path(image).name, "iteration": i, "warmup": i < args.warmup}
                row.update(_run_once(image))
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                out.flush()
                print(f"{args.label} {row['image']} #{i} {row['ocrMs']} ms", flush=True)
        out.write(json.dumps({"type": "end", "maxRssMb": _max_rss_mb()}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

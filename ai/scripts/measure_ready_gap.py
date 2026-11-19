"""ready와 검색기 구축 사이 공백 측정(이슈 #98).

실제 앱(lifespan → 모델 로드 → 검색기 구축)을 127.0.0.1의 지정 포트로 띄우고 0.5초마다
``/health/ready``를 읽는다. 같은 순간 채팅 라우트와 같은 방식(요청 시점의
``app.state.rag.finance_policy_search``)으로 ``search_finance_policy`` 도구를 직접 부른다.
남기는 값: ready가 처음 true가 된 시각, 금융 정책 검색기 구축 시작·끝 시각, 도구가 처음 성공한 시각,
ready=true였는데 도구가 실패한 표본 수와 구간.

- LLM·Spring·MongoDB를 부르지 않는다(유료 호출 0). HF는 ``HF_HUB_OFFLINE=1``(로컬 캐시만).
- Spring·내부 토큰은 ready 판정용 더미 값을 env로 준다(호출하지 않음).
  실행 폴더의 .env보다 env가 우선한다.
- MongoDB·벡터 인덱스 확인은 이 스크립트가 바꾸지 않는다. 로컬에 Atlas 벡터 인덱스가 없으면 ready가
  끝까지 false다. #98 측정에서 이 확인을 어떻게 다뤘는지는
  ``specs/rag/ready_search_status_v1_results.md``.
- 캐시: ``--artifacts-dir`` 아래 ``embedding-cache``를 쓴다. 빈 폴더면 cold, 한 번 돌린 폴더면 warm.

    python scripts/measure_ready_gap.py --corpus-dir <금융 정책 문서 폴더> \
        --artifacts-dir <캐시 폴더> --label before-cold --out <결과.json>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401

EMBEDDING_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
PROBE_QUERY = "외국인 해외송금 한도"
INTERVAL_SECONDS = 0.5
# 구축이 끝난 뒤 이만큼 더 표본을 모은다(성공으로 바뀐 것을 확인).
TAIL_SECONDS = 3.0
MAX_SECONDS = 1800.0


def _env(corpus_dir: str, artifacts_dir: str) -> dict[str, str]:
    return {
        "LOAD_MODEL_ON_STARTUP": "true",
        "FINANCE_POLICY_CORPUS_DIR": corpus_dir,
        "PRODUCT_CORPUS_DIR": "",
        "RAG_ARTIFACTS_DIR": artifacts_dir,
        "EMBEDDING_MODEL_REVISION": EMBEDDING_REVISION,
        "SPRING_INTERNAL_API_URL": "http://127.0.0.1:9",
        "SPRING_INTERNAL_API_KEY": "measure-dummy-key-not-used-0000000000",
        "AI_INTERNAL_API_TOKEN": "measure-dummy-token-not-used-00000000000",
        "MONGO_URI": "",
        "HF_HUB_OFFLINE": "1",
        "LOG_LEVEL": "WARNING",
    }


def _get_ready(port: int) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health/ready", timeout=5) as r:
            body: dict[str, Any] = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = json.loads(e.read())
    return body


def run(corpus_dir: str, artifacts_dir: str, label: str, port: int) -> dict[str, Any]:
    os.environ.update(_env(corpus_dir, artifacts_dir))
    started = time.monotonic()

    def now() -> float:
        return round(time.monotonic() - started, 2)

    import uvicorn

    from app import main
    from app.chat.finance_policy_tools import SEARCH_FINANCE_POLICY, FinancePolicyTool

    events: dict[str, float] = {}
    original_build = main._build_finance_policy_search

    def timed_build(state: Any) -> None:
        events["build_start"] = now()
        original_build(state)
        events["build_end"] = now()

    main._build_finance_policy_search = timed_build

    config = uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, name="measure-server", daemon=True).start()

    async def probe() -> str:
        tool = FinancePolicyTool(main.app.state.rag.finance_policy_search, timeout=10)
        out = await tool.run(SEARCH_FINANCE_POLICY, {"queryKo": PROBE_QUERY})
        return f"{out.status}:{out.error_code or ''}"

    while not server.started:
        time.sleep(0.05)
    events["server_up"] = now()
    samples: list[dict[str, Any]] = []
    while True:
        t = now()
        body = _get_ready(port)
        tool = asyncio.run(probe())
        samples.append({"t": t, "ready": body["ready"], "checks": body["checks"], "tool": tool})
        if body["ready"]:
            events.setdefault("ready_first", t)
        if body["checks"].get("model") == "loaded":
            events.setdefault("model_loaded", t)
        if tool.startswith("SUCCEEDED"):
            events.setdefault("tool_first_ok", t)
        if "build_end" in events and now() - events["build_end"] > TAIL_SECONDS:
            break
        if now() > MAX_SECONDS:
            break
        time.sleep(INTERVAL_SECONDS)
    server.should_exit = True

    gap = [s for s in samples if s["ready"] and not s["tool"].startswith("SUCCEEDED")]
    summary = {
        "label": label,
        "events": events,
        "ready_true_but_tool_failed_samples": len(gap),
        "ready_true_but_tool_failed_span": [gap[0]["t"], gap[-1]["t"]] if gap else None,
        "tool_outcomes_while_ready": sorted({s["tool"] for s in gap}),
        "total_samples": len(samples),
    }
    return {"summary": summary, "samples": samples}


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus-dir", required=True)
    parser.add_argument("--artifacts-dir", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--out", required=True, type=Path)
    # 공용 8000·8080을 피한다.
    parser.add_argument("--port", type=int, default=8098)
    args = parser.parse_args(argv)
    result = run(args.corpus_dir, args.artifacts_dir, args.label, args.port)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()

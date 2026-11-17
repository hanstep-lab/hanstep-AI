"""scripts/product_app_smoke.py: 가짜 Spring(MockTransport)으로 기록 값 확인(네트워크 없음)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.unit

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
TOKEN = "test-token-not-real"


def _script() -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "product_app_smoke", SCRIPTS / "product_app_smoke.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SMOKE = _script()


def _sse(*events: tuple[str, Any]) -> str:
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)


RECOMMEND = _sse(
    ("chat.started", {"messageId": 1}),
    ("chat.tool_call", {"toolName": "recommend_savings_products", "status": "SUCCEEDED"}),
    ("chat.delta", {"text": "Try 신한 땡겨요 적금 or 정기적금. "}),
    ("chat.delta", {"text": "Your visa and age were not checked."}),
    (
        "chat.sources",
        {
            "sources": [
                {"type": "PRODUCT_DOCUMENT", "chunkRef": "doc-de733331abd3"},
                {"type": "PRODUCT_DOCUMENT", "chunkRef": "doc-ad95d3f72e39"},
                {"type": "POLICY_DOCUMENT", "chunkRef": "x"},
            ]
        },
    ),
    ("chat.done", {"content": "ignored"}),
)
FAILED_TOOL = _sse(
    (
        "chat.tool_call",
        {
            "toolName": "recommend_savings_products",
            "status": "FAILED",
            "errorCode": "SPRING_HTTP_404",
        },
    ),
    ("chat.delta", {"text": "I couldn't load your data."}),
    ("chat.done", {}),
)


def _client(streams: list[str], seen: list[httpx.Request]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        if request.url.path == "/api/v1/chat/sessions":
            return httpx.Response(201, json={"id": len(seen)})
        assert request.url.path.endswith("/messages/stream")
        assert json.loads(request.content)["content"]
        return httpx.Response(200, text=streams.pop(0))

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_records_tool_sources_and_answer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[httpx.Request] = []
    client = _client([RECOMMEND, FAILED_TOOL], seen)
    argv = ["--base-url", "http://spring.test/", "--token", TOKEN, "--ids", "Q01,Q13"]
    assert SMOKE.main([*argv, "--output-dir", str(tmp_path), "--label", "t"], client=client) == 0
    # 문항마다 대화방을 새로 만든다.
    assert [r.url.path for r in seen].count("/api/v1/chat/sessions") == 2
    files = list(tmp_path.glob("product_app_smoke-t-*.jsonl"))
    assert len(files) == 1
    lines = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    meta, first, second = lines
    assert meta["ids"] == ["Q01", "Q13"]
    assert first["status"] == "done"
    assert first["productToolCalled"] and first["productToolStatus"] == ["SUCCEEDED"]
    assert first["productDocuments"] == 2
    assert first["answerCatalogNames"] == ["신한 땡겨요 적금", "정기적금"]
    assert first["mentionsUnchecked"] is True
    assert second["productToolErrorCode"] == ["SPRING_HTTP_404"]
    assert second["productDocuments"] == 0
    assert second["expectedOutcome"]["reason"] == "REQUEST_EXCEEDS_SURPLUS"
    out = capsys.readouterr().out
    assert "| Q01 |" in out and "| Q13 |" in out
    # 토큰은 출력·파일 어디에도 남기지 않는다.
    assert TOKEN not in out and TOKEN not in files[0].read_text(encoding="utf-8")


def test_error_and_http_status(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    error = _sse(("chat.error", {"code": "AGENT_TIMEOUT", "message": "x"}))

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/v1/chat/sessions":
            return httpx.Response(201, json={"id": 7})
        return httpx.Response(200, text=error) if len(seen) == 2 else httpx.Response(503)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    rows = [SMOKE.ask(client, "http://spring.test", TOKEN, {"id": "Q", "text": "hi"})]
    assert rows[0]["status"] == "error" and rows[0]["errorCode"] == "AGENT_TIMEOUT"
    rows.append(SMOKE.ask(client, "http://spring.test", TOKEN, {"id": "R", "text": "hi"}))
    assert rows[1]["status"] == "http_error" and rows[1]["httpStatus"] == 503


def test_dry_run_makes_no_calls(capsys: pytest.CaptureFixture[str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no calls in --dry-run")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    argv = ["--base-url", "http://spring.test", "--token", TOKEN, "--dry-run"]
    assert SMOKE.main(argv, client=client) == 0
    out = capsys.readouterr().out
    assert "10 questions, no calls made" in out
    for qid in SMOKE.SMOKE_IDS:
        assert qid in out


def test_base_url_and_token_are_required() -> None:
    with pytest.raises(SystemExit):
        SMOKE.main(["--token", TOKEN])
    with pytest.raises(SystemExit):
        SMOKE.main(["--base-url", "http://spring.test"])

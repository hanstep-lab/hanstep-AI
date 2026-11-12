"""INT-05 chat stream: event order and format, thinking filter, config checks, failures.

No real Anthropic or Spring call: fake model, fixture/fake context sources, MockTransport.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from app.api.routes.chat import build_model, finance_source
from app.chat.fake_llm import FAKE_MODEL_ID, TRIGGER_EMPTY_ANSWER, TRIGGER_LLM_ERROR, FakeChatModel
from app.chat.schemas import ChatStreamRequest, LlmConfig
from app.chat.stream import CALL_SEPARATOR, ChatRunOptions, run_chat
from app.chat.tools import (
    FinanceTool,
    FixtureFinanceContext,
    SpringFinanceContext,
)
from app.core.config import Settings, check_chat_config
from app.integrations.policy_gateway import UnconfiguredPolicyGateway
from app.integrations.spring_client import SpringApiError, SpringInternalClient
from app.main import AppState, create_app
from app.rag.embeddings import BgeM3Embedder

pytestmark = pytest.mark.unit

TOKEN = "f" * 40
STREAM = "/internal/v1/agent/chat/stream"
TOOL_EVENT_KEYS = {
    "order",
    "toolName",
    "arguments",
    "status",
    "resultRef",
    "latencyMs",
    "errorCode",
}


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "ai_internal_api_token": TOKEN,
        "chat_fake_delta_delay_ms": 0,
        # 기본값(AGENT_MODEL)이 바뀌어도 테스트가 흔들리지 않게 고정한다.
        # override 테스트가 haiku로 바꾸므로 기본은 그와 다른 모델로 둔다.
        "agent_model": "claude-sonnet-5-5",
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def _client(**overrides: Any) -> TestClient:
    app = create_app()
    app.state.rag = AppState(
        settings=_settings(**overrides),
        gateway=UnconfiguredPolicyGateway(),
        embedder=BgeM3Embedder(),
    )
    return TestClient(app)


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "runId": "run_" + "a" * 32,
        "userId": 1,
        "sessionId": 9,
        "language": "en",
        "history": [],
        "message": "How much money do I have left each month?",
    }
    body.update(overrides)
    return body


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        name, data = None, []
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data.append(line[6:])
        if name:
            assert len(data) == 1, "data must be one JSON line"
            events.append((name, json.loads(data[0])))
    return events


def _post(client: TestClient, body: dict[str, Any], **headers: str) -> httpx.Response:
    return client.post(STREAM, json=body, headers={"X-Internal-Token": TOKEN, **headers})


class RecordingSource:
    def __init__(self, result: Any = None, error: BaseException | None = None, delay: float = 0):
        self.calls: list[tuple[int, str | None]] = []
        self.result = result
        self.error = error
        self.delay = delay

    async def fetch(self, user_id: int, request_id: str | None) -> dict[str, Any]:
        self.calls.append((user_id, request_id))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return dict(self.result)
        return await FixtureFinanceContext().fetch(user_id, request_id)


async def _run(
    message: str = "How much is left?",
    *,
    model: FakeChatModel | None = None,
    source: Any = None,
    user_id: int = 1,
    run_timeout: float = 30,
    tool_timeout: float = 5,
    max_tool_calls: int = 3,
    metrics: bool = False,
) -> list[tuple[str, dict[str, Any]]]:
    request = ChatStreamRequest.model_validate(_body(message=message, userId=user_id))
    tool = FinanceTool(
        source or FixtureFinanceContext(), user_id=user_id, request_id="req_t", timeout=tool_timeout
    )
    events = []
    async for event in run_chat(
        request,
        config=LlmConfig.from_settings(_settings()),
        model=model or FakeChatModel(delta_delay_ms=0),
        tool=tool,
        options=ChatRunOptions(max_tool_calls, run_timeout, metrics, FAKE_MODEL_ID),
    ):
        assert event.raw_data is not None and "\n" not in event.raw_data
        events.append((str(event.event), json.loads(event.raw_data)))
    return events


def _names(events: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [name for name, _ in events]


# --- API: order, format, headers -------------------------------------------------------------


def test_stream_order_and_format() -> None:
    response = _post(_client(), _body())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    names = _names(events)
    assert names[0] == "agent.tool_call"
    assert names[-2:] == ["agent.sources", "agent.done"]
    assert set(names[1:-2]) == {"agent.delta"}
    assert "agent.metrics" not in names

    tool = events[0][1]
    assert set(tool) == TOOL_EVENT_KEYS
    assert tool["order"] == 1
    assert tool["toolName"] == "get_financial_analysis"
    assert tool["arguments"] == {}
    assert tool["status"] == "SUCCEEDED"
    assert tool["resultRef"] == {"type": "FINANCIAL_ANALYSIS", "id": 7}
    assert tool["errorCode"] is None

    deltas = "".join(data["text"] for name, data in events if name == "agent.delta")
    done = events[-1][1]
    assert done["content"] == deltas
    assert "KRW 675,317" in done["content"]
    assert done["blocks"] is None
    assert done["modelId"] == FAKE_MODEL_ID
    assert done["promptVersion"] == "chat-v2"
    assert done["usage"] == {
        "inputTokens": 0,
        "outputTokens": 0,
        "cacheReadInputTokens": 0,
        "cacheCreationInputTokens": 0,
        "llmCalls": 2,
    }
    assert set(done["timings"]) == {"firstDeltaMs", "toolMs", "totalMs"}
    assert events[-2][1] == {
        "sources": [{"type": "FINANCIAL_ANALYSIS", "id": 7, "chunkRef": None, "pageLabel": None}]
    }


def test_metrics_event_only_with_header() -> None:
    client = _client()
    events = parse_sse(_post(client, _body(), **{"X-Chat-Metrics": "1"}).text)
    names = _names(events)
    assert names[-2:] == ["agent.metrics", "agent.done"]
    metrics = events[-2][1]
    assert metrics["config"]["model"] == "claude-sonnet-5-5"
    assert metrics["config"]["promptVersion"] == "chat-v2"
    assert [c["stopReason"] for c in metrics["calls"]] == ["tool_use", "end_turn"]
    assert set(metrics["calls"][0]) == {
        "model",
        "stopReason",
        "inputTokens",
        "outputTokens",
        "cacheReadInputTokens",
        "cacheCreationInputTokens",
        "thinkingBlocks",
        "firstTextMs",
        "durationMs",
    }
    assert "agent.metrics" not in _names(parse_sse(_post(client, _body()).text))


def test_config_override_ignored_unless_allowed() -> None:
    override = json.dumps({"model": "claude-haiku-5-5", "thinking": "disabled", "effort": "low"})
    headers = {"X-Chat-Metrics": "1", "X-Chat-Config": override}

    ignored = parse_sse(_post(_client(), _body(), **headers).text)
    assert ignored[-2][1]["config"]["model"] == "claude-sonnet-5-5"

    applied = parse_sse(_post(_client(chat_allow_config_override=True), _body(), **headers).text)
    assert applied[-2][1]["config"] == {
        "model": "claude-haiku-5-5",
        "thinking": "disabled",
        "effort": "low",
        "maxTokens": 2000,
        "promptVersion": "chat-v2",
    }


def test_bad_config_override_is_400_before_stream() -> None:
    client = _client(chat_allow_config_override=True)
    for raw in ('{"model":"claude-sonnet-5-5","thinking":"disabled"}', "not json", '{"x":1}'):
        response = _post(client, _body(), **{"X-Chat-Config": raw})
        assert response.status_code == 400
        assert response.json()["code"] == "VALIDATION_ERROR"


def test_missing_token_is_401() -> None:
    response = _client().post(STREAM, json=_body())
    assert response.status_code == 401


@pytest.mark.parametrize(
    "overrides",
    [
        {"runId": ""},
        {"runId": "r" * 65},
        {"language": "fr"},
        {"message": ""},
        {"message": "m" * 2001},
        {"history": [{"role": "USER", "content": "q"}] * 7},
        {"history": [{"role": "SYSTEM", "content": "q"}]},
        {"userId": 0},
        {"extra": 1},
    ],
)
def test_invalid_request_is_400_envelope(overrides: dict[str, Any]) -> None:
    response = _post(_client(), _body(**overrides))
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_anthropic_mode_without_key_is_503() -> None:
    response = _post(_client(chat_llm_mode="anthropic"), _body())
    assert response.status_code == 503


# --- graph behaviour -------------------------------------------------------------------------


async def test_thinking_is_filtered_and_kept_across_tool_round_trip() -> None:
    model = FakeChatModel(delta_delay_ms=0, emit_thinking=True)
    events = await _run(model=model, metrics=True)

    deltas = [data["text"] for name, data in events if name == "agent.delta"]
    assert deltas and all("plan" not in d for d in deltas)
    assert events[-1][1]["content"] == "".join(deltas)
    metrics = next(data for name, data in events if name == "agent.metrics")
    assert [c["thinkingBlocks"] for c in metrics["calls"]] == [1, 1]
    # Second LLM call receives the tool-calling assistant message with its thinking block intact.
    second_call = model.probe["inputs"][1]
    tool_message = next(m for m in second_call if isinstance(m, AIMessage) and m.tool_calls)
    assert isinstance(tool_message.content, list)
    thinking = [b for b in tool_message.content if b.get("type") == "thinking"]
    assert thinking[0]["thinking"] == "plan"
    assert thinking[0]["signature"] == "fake-sig"


async def test_empty_answer_ends_with_agent_error() -> None:
    events = await _run(TRIGGER_EMPTY_ANSWER)
    assert _names(events)[-1] == "agent.error"
    assert events[-1][1]["code"] == "AGENT_INTERNAL_ERROR"
    assert "agent.done" not in _names(events)


async def test_llm_error_ends_with_llm_upstream_error() -> None:
    events = await _run(TRIGGER_LLM_ERROR)
    assert _names(events) == ["agent.error"]
    assert events[0][1]["code"] == "LLM_UPSTREAM_ERROR"
    assert "Error" not in events[0][1]["message"]


async def test_run_timeout_is_agent_timeout() -> None:
    events = await _run(model=FakeChatModel(delta_delay_ms=200), run_timeout=0.3)
    assert _names(events)[-1] == "agent.error"
    assert events[-1][1]["code"] == "AGENT_TIMEOUT"


async def test_closing_the_stream_cancels_the_llm_call() -> None:
    model = FakeChatModel(delta_delay_ms=100)
    request = ChatStreamRequest.model_validate(_body())
    tool = FinanceTool(FixtureFinanceContext(), user_id=1, request_id=None, timeout=5)
    stream = run_chat(
        request,
        config=LlmConfig.from_settings(_settings()),
        model=model,
        tool=tool,
        options=ChatRunOptions(3, 30),
    )
    async for event in stream:
        if event.event == "agent.delta":
            break
    await stream.aclose()  # what a Spring disconnect does to the generator
    await asyncio.sleep(0.05)
    assert model.probe.get("cancelled") is True


async def test_max_tool_calls_zero_answers_without_tools() -> None:
    events = await _run(max_tool_calls=0)
    assert "agent.tool_call" not in _names(events)
    assert _names(events)[-1] == "agent.done"


# --- tool -----------------------------------------------------------------------------------


async def test_tool_uses_only_the_request_user_id() -> None:
    source = RecordingSource()
    tool = FinanceTool(source, user_id=42, request_id="req_x", timeout=5)
    outcome = await tool.run("get_financial_analysis", {"userId": 2})
    assert source.calls == [(42, "req_x")]
    assert outcome.status == "SUCCEEDED"
    assert '"id"' not in outcome.content and "isMock" not in outcome.content

    stream_source = RecordingSource()
    await _run(source=stream_source, user_id=77)
    assert [user for user, _ in stream_source.calls] == [77]


async def test_tool_result_for_llm_has_only_needed_values() -> None:
    outcome = await FinanceTool(FixtureFinanceContext(), user_id=1, request_id=None, timeout=5).run(
        "get_financial_analysis", {}
    )
    data = json.loads(outcome.content)
    assert set(data) == {"currency", "analysis", "declaredCommitments", "goals"}
    assert data["analysis"]["spare"] == 675317
    assert data["analysis"]["variableExpense"] == {"avg": 454683, "min": 410600, "max": 512400}
    assert data["analysis"]["observedMonthCount"] == 12
    assert data["declaredCommitments"]["deductibleTotal"] == 1670000
    assert data["goals"] == [
        {
            "goalType": "LUMP_SUM",
            "targetAmount": 6000000,
            "targetDate": "2027-09-30",
            "status": "ACTIVE",
            "monthlyRequired": 600000,
        }
    ]


@pytest.mark.parametrize(
    ("source", "status", "code"),
    [
        (
            RecordingSource(error=SpringApiError(500, "INTERNAL_ERROR", "")),
            "FAILED",
            "SPRING_HTTP_500",
        ),
        (RecordingSource(error=httpx.ConnectError("down")), "FAILED", "SPRING_ERROR"),
        (RecordingSource(delay=0.5), "TIMEOUT", "SPRING_TIMEOUT"),
    ],
)
async def test_spring_failure_still_answers(
    source: RecordingSource, status: str, code: str
) -> None:
    events = await _run(source=source, tool_timeout=0.1)
    tool = events[0][1]
    assert (tool["status"], tool["errorCode"], tool["resultRef"]) == (status, code, None)
    assert _names(events)[-1] == "agent.done"
    assert "agent.sources" not in _names(events)
    assert "could not load" in events[-1][1]["content"]


async def test_no_analysis_succeeds_without_ref() -> None:
    source = RecordingSource(
        result={"userId": 1, "latestAnalysis": None, "commitments": None, "goals": []}
    )
    events = await _run(source=source)
    assert events[0][1]["status"] == "SUCCEEDED"
    assert events[0][1]["resultRef"] is None
    assert "agent.sources" not in _names(events)
    assert "MyData" in events[-1][1]["content"]


def test_spring_client_finance_context_get() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"userId": 5, "latestAnalysis": None})

    client = SpringInternalClient(
        base_url="http://spring.test",
        api_key="s" * 40,
        client=httpx.Client(base_url="http://spring.test", transport=httpx.MockTransport(handler)),
    )
    assert client.finance_context(5, timeout=5, request_id="req_1") == {
        "userId": 5,
        "latestAnalysis": None,
    }
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/internal/v1/ai-context/users/5/finance"
    assert seen[0].headers["X-Internal-Token"] == "s" * 40
    assert seen[0].headers["X-Request-Id"] == "req_1"


def test_finance_source_uses_spring_only_in_anthropic_mode_with_spring() -> None:
    def request_for(settings: Settings) -> Any:
        state = SimpleNamespace(settings=settings, chat_spring=None)
        return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(rag=state)))

    spring = {"spring_internal_api_url": "http://spring.test", "spring_internal_api_key": "s" * 40}
    assert isinstance(finance_source(request_for(_settings(**spring))), FixtureFinanceContext)
    assert isinstance(
        finance_source(request_for(_settings(chat_llm_mode="anthropic"))), FixtureFinanceContext
    )
    assert isinstance(
        finance_source(request_for(_settings(chat_llm_mode="anthropic", **spring))),
        SpringFinanceContext,
    )


# --- thinking / effort configuration ---------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "thinking", "effort", "ok"),
    [
        ("claude-sonnet-5-5", "model_default", "", True),
        ("claude-sonnet-5-5", "adaptive", "high", True),
        ("claude-sonnet-5-5", "between_tools", "high", True),
        ("claude-sonnet-5-5", "disabled", "", False),
        ("claude-haiku-5-5", "disabled", "low", True),
        ("claude-haiku-5-5", "between_tools", "", False),
        ("claude-haiku-5-5", "model_default", "medium", True),
    ],
)
def test_thinking_effort_combinations(model: str, thinking: str, effort: str, ok: bool) -> None:
    if ok:
        check_chat_config(model, thinking, effort)
        _settings(agent_model=model, chat_thinking=thinking, chat_effort=effort)
    else:
        with pytest.raises(ValueError, match="does not accept"):
            check_chat_config(model, thinking, effort)
        with pytest.raises(ValidationError):
            _settings(agent_model=model, chat_thinking=thinking, chat_effort=effort)
        with pytest.raises(ValueError):
            LlmConfig.from_settings(_settings()).with_override(
                json.dumps({"model": model, "thinking": thinking, "effort": effort})
            )


def test_chat_anthropic_receives_thinking_and_effort_without_calling_api() -> None:
    settings = _settings(chat_llm_mode="anthropic", anthropic_api_key="sk-test-not-real")
    config = LlmConfig("claude-haiku-5-5", "disabled", "low", 1500)
    model = build_model(settings, config)
    payload = model._get_request_payload([("human", "hi")])  # type: ignore[attr-defined]
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["output_config"] == {"effort": "low"}
    assert payload["max_tokens"] == 1500
    assert model.max_retries == 1  # type: ignore[attr-defined]

    default = build_model(settings, LlmConfig("claude-sonnet-5-5", "model_default", "", 2000))
    payload = default._get_request_payload([("human", "hi")])  # type: ignore[attr-defined]
    assert "thinking" not in payload
    assert "output_config" not in payload


# --- metrics on failed runs and first-text timings -------------------------------------------


async def test_metrics_come_before_error_with_calls_so_far() -> None:
    events = await _run(TRIGGER_EMPTY_ANSWER, metrics=True)
    assert _names(events)[-2:] == ["agent.metrics", "agent.error"]
    metrics = events[-2][1]
    assert [c["stopReason"] for c in metrics["calls"]] == ["tool_use", "max_tokens"]
    assert metrics["firstAnswerDeltaMs"] is None
    # agent.error itself is unchanged (Spring contract).
    assert set(events[-1][1]) == {"code", "message"}

    failed = await _run(TRIGGER_LLM_ERROR, metrics=True)
    assert _names(failed) == ["agent.metrics", "agent.error"]
    assert failed[0][1]["calls"] == []

    assert "agent.metrics" not in _names(await _run(TRIGGER_EMPTY_ANSWER))


async def test_first_answer_delta_and_pre_tool_text() -> None:
    plain = await _run(metrics=True)
    metrics = next(data for name, data in plain if name == "agent.metrics")
    done = plain[-1][1]
    assert metrics["preToolTextChars"] == 0
    assert metrics["firstAnswerDeltaMs"] == done["timings"]["firstDeltaMs"]

    model = FakeChatModel(delta_delay_ms=0, pre_tool_text="Let me check. ")
    chatty = await _run(model=model, metrics=True)
    metrics = next(data for name, data in chatty if name == "agent.metrics")
    done = chatty[-1][1]
    assert metrics["preToolTextChars"] == len("Let me check. ")
    assert metrics["firstAnswerDeltaMs"] >= done["timings"]["firstDeltaMs"]
    # The text before the tool call still streams, and the answer starts after a blank line.
    assert _names(chatty)[0] == "agent.delta"
    texts = [data["text"] for name, data in chatty if name == "agent.delta"]
    assert "".join(texts) == done["content"]
    assert done["content"] == "Let me check. " + CALL_SEPARATOR + plain[-1][1]["content"]


def _llm_call(call: int, stop: str) -> dict[str, Any]:
    return {"type": "llm_call", "call": call, "model": FAKE_MODEL_ID, "stopReason": stop,
            "inputTokens": 0, "outputTokens": 0, "cacheReadInputTokens": 0,
            "cacheCreationInputTokens": 0, "thinkingBlocks": 0, "firstTextMs": None,
            "durationMs": 0}  # fmt: skip


class _ScriptedGraph:
    """Stands in for the compiled graph: yields the given custom-stream items as they are."""

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = items

    async def astream(self, *_: Any, **__: Any) -> Any:
        for item in self.items:
            yield item


def _delta(text: str, call: int) -> dict[str, Any]:
    return {"type": "delta", "text": text, "call": call}


@pytest.mark.parametrize(
    ("items", "texts"),
    [
        # Text before the tool call, then the answer's first chunk is blank: dropped, and the
        # separator goes on the first real text only once.
        (
            [
                _delta("Let me check.", 0),
                _llm_call(0, "tool_use"),
                _delta(" ", 1),
                _delta("Your", 1),
                _delta(" spare", 1),
                _llm_call(1, "end_turn"),
            ],
            ["Let me check.", CALL_SEPARATOR + "Your", " spare"],
        ),
        # A blank chunk in the middle of the answer is kept, so words stay apart.
        (
            [
                _delta("Let me check.", 0),
                _llm_call(0, "tool_use"),
                _delta("Your", 1),
                _delta(" ", 1),
                _delta("spare", 1),
                _llm_call(1, "end_turn"),
            ],
            ["Let me check.", CALL_SEPARATOR + "Your", " ", "spare"],
        ),
        # No text before the tool call: no separator, blank chunks kept as they are.
        (
            [
                _llm_call(0, "tool_use"),
                _delta("Your", 1),
                _delta(" ", 1),
                _delta("spare", 1),
                _llm_call(1, "end_turn"),
            ],
            ["Your", " ", "spare"],
        ),
    ],  # fmt: skip
    ids=["blank-first-answer-chunk", "blank-mid-answer-chunk", "no-pre-tool-text"],
)
async def test_call_separator_on_graph_output(
    monkeypatch: pytest.MonkeyPatch, items: list[dict[str, Any]], texts: list[str]
) -> None:
    monkeypatch.setattr("app.chat.stream.build_graph", lambda *_, **__: _ScriptedGraph(items))
    events = await _run()
    assert [data["text"] for name, data in events if name == "agent.delta"] == texts
    assert events[-1][0] == "agent.done"
    assert events[-1][1]["content"] == "".join(texts)

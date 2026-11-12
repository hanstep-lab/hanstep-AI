"""Chat agent measurement: latency, tokens, cost and unsupported numbers on the same questions.

Not part of pytest or CI. Calls a running server; it never calls Anthropic itself.

    # INT-05 directly (tokens and agent.metrics); server needs CHAT_ALLOW_CONFIG_OVERRIDE=true
    # when --variants is given. --llm-mode says what the server runs (default fake).
    uv run python scripts/evaluate_chat.py --target ai --llm-mode anthropic --questions F01,F03 \
        --output artifacts/chat/haiku_check.jsonl
    # Spring CHT-04 end to end (latency only)
    uv run python scripts/evaluate_chat.py --target spring --jwt <token> --subset latency \
        --output artifacts/chat/spring_latency.jsonl
    # Re-score a saved run without any API call
    uv run python scripts/evaluate_chat.py --rescore artifacts/chat/run.jsonl \
        --output artifacts/chat/run_rescored.jsonl

Writes one JSONL line per call as it goes (the first line holds the conditions) and a summary
markdown next to it (same name, .md). Variants run round-robin inside each question and
repeat, so time-of-day drift spreads evenly. --max-usd stops before a call that would likely
cross the budget; runs that end in agent.error count too (usage from agent.metrics). With
--llm-mode fake the run stops at once if the server answers with a real model or real tokens.
Questions with referenceValues get a referenceCheck (reference / wrong_number / no_value), and
each row keeps the arrival ms of every delta (deltaTimesMs) for replaying the stream later.
Judgment questions (expectedConclusion) are listed with their full answers for a person to grade.
With judgmentFacts on, the number check also allows the judgment facts the server added
(app/chat/judgment.py on the same summary and question).
The API key and internal tokens are never printed or written.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import statistics
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401
import httpx

from app.chat.fake_llm import FAKE_MODEL_ID
from app.chat.judgment import judgment_facts
from app.chat.number_check import check_answer, rounded_values, scan_numbers
from app.chat.tools import FIXTURE, summarize
from app.core.config import Settings, get_settings
from app.integrations.spring_client import SpringInternalClient
from app.rag.cli import EXIT_BLOCKED, EXIT_OK

AI_ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = AI_ROOT / "specs" / "chat" / "eval_questions_v1.json"
EXIT_INTERRUPTED = 130
EXIT_USAGE = 2

# USD per 1M tokens: input, output (thinking included), cache read, cache write.
# 2026-11-11 official Anthropic prices.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.10, 2.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}
# Upper-bound guess for the plan only: two LLM calls (tool call + answer), each with this
# much input and the full max_tokens of output. Real usage replaces it after the first call.
EST_INPUT_TOKENS_PER_LLM_CALL = 3000
EST_LLM_CALLS = 2
USAGE_KEYS = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")


def price_for(model: str | None) -> tuple[float, float, float, float]:
    if not model or model == FAKE_MODEL_ID:
        return (0.0, 0.0, 0.0, 0.0)
    for prefix, price in PRICES.items():
        if model.startswith(prefix):
            return price
    # Unknown model: assume the dearest known price so the budget guard stays safe.
    return PRICES["claude-sonnet-5-5"]


def cost_usd(usage: dict[str, Any] | None, model: str | None) -> float:
    if not usage:
        return 0.0
    p_in, p_out, p_read, p_write = price_for(model)
    return (
        float(
            usage.get("inputTokens", 0) * p_in
            + usage.get("outputTokens", 0) * p_out
            + usage.get("cacheReadInputTokens", 0) * p_read
            + usage.get("cacheCreationInputTokens", 0) * p_write
        )
        / 1_000_000
    )


def estimate_call_usd(model: str, max_tokens: int, fake: bool) -> float:
    if fake:
        return 0.0
    p_in, p_out, _, _ = price_for(model)
    per_llm = EST_INPUT_TOKENS_PER_LLM_CALL * p_in + max_tokens * p_out
    return EST_LLM_CALLS * per_llm / 1_000_000


def run_usage(done: dict[str, Any], metrics: dict[str, Any] | None) -> dict[str, Any] | None:
    """agent.done usage; for a run that ended in agent.error, the sum over agent.metrics calls."""
    if done.get("usage"):
        usage: dict[str, Any] = done["usage"]
        return usage
    calls = (metrics or {}).get("calls") or []
    if not calls:
        return None
    summed: dict[str, Any] = {k: sum(int(c.get(k) or 0) for c in calls) for k in USAGE_KEYS}
    summed["llmCalls"] = len(calls)
    return summed


def answering_model(done: dict[str, Any], metrics: dict[str, Any] | None) -> str | None:
    """The model that actually answered (done.modelId or the last call's model), if known."""
    models = [c["model"] for c in (metrics or {}).get("calls") or [] if c.get("model")]
    model: str | None = done.get("modelId") or (models[-1] if models else None)
    return model


def load_questions(path: Path = QUESTIONS) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def select_questions(
    spec: dict[str, Any], ids: str | None, subset: str | None, parser: argparse.ArgumentParser
) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = spec["questions"]
    if subset == "latency":
        keep = set(spec["latencySubset"])
        questions = [q for q in questions if q["id"] in keep]
    if ids:
        wanted = {part.strip() for part in ids.split(",") if part.strip()}
        unknown = sorted(wanted - {q["id"] for q in questions})
        if not wanted or unknown:
            parser.error(f"unknown or empty --questions: {unknown}")
        questions = [q for q in questions if q["id"] in wanted]
    return questions


def load_variants(path: Path | None, settings: Settings) -> list[dict[str, Any]]:
    if path is None:
        return [
            {
                "name": "server-default",
                "model": settings.chat_model_id,
                "thinking": settings.chat_thinking,
                "effort": settings.chat_effort,
                "maxTokens": settings.chat_max_tokens,
                "judgmentFacts": settings.chat_judgment_facts,
                "promptVersion": settings.chat_prompt_version,
                "override": False,
            }
        ]
    variants: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    names = [v.get("name") for v in variants]
    if not variants or any(not n for n in names) or len(set(names)) != len(names):
        raise ValueError("--variants needs a non-empty list with unique names")
    for v in variants:
        v.setdefault("model", settings.chat_model_id)
        v.setdefault("maxTokens", settings.chat_max_tokens)
        v.setdefault("judgmentFacts", settings.chat_judgment_facts)
        v.setdefault("promptVersion", settings.chat_prompt_version)
        v["override"] = True
    return variants


def config_header(variant: dict[str, Any]) -> str | None:
    if not variant.get("override"):
        return None
    keys = ("model", "thinking", "effort", "maxTokens", "judgmentFacts", "promptVersion")
    return json.dumps({k: variant[k] for k in keys if variant.get(k) is not None})


def tool_context(settings: Settings, user_id: int, llm_mode: str) -> dict[str, Any]:
    """The INT-06 data the server's tool sees, summarized the same way (for number checks)."""
    if llm_mode == "anthropic" and settings.spring_configured():
        client = SpringInternalClient(
            base_url=settings.spring_internal_api_url,
            api_key=settings.spring_internal_api_key.get_secret_value(),
            header=settings.internal_token_header,
        )
        try:
            context = client.finance_context(user_id, timeout=10)
        finally:
            client.close()
    else:
        context = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if context.get("latestAnalysis") is None:
        return {"analysis": None}
    return summarize(context)


def iter_sse(lines: Iterator[str]) -> Iterator[tuple[str, dict[str, Any]]]:
    name: str | None = None
    data: list[str] = []
    for line in lines:
        if line == "":
            if name and data:
                yield name, json.loads("\n".join(data))
            name, data = None, []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())


def stream_call(
    client: httpx.Client, url: str, body: dict[str, Any], headers: dict[str, str], prefix: str
) -> dict[str, Any]:
    """POST and read the SSE stream. Times are monotonic ms from the request start."""
    started = time.perf_counter()

    def ms() -> int:
        return round((time.perf_counter() - started) * 1000)

    result: dict[str, Any] = {"events": [], "timings": {}, "deltas": [], "deltaMs": [], "tools": []}
    with client.stream("POST", url, json=body, headers=headers, timeout=180) as response:
        result["httpStatus"] = response.status_code
        if response.status_code != 200:
            response.read()
            result["status"] = "http_error"
            try:
                result["errorCode"] = response.json().get("code")
            except ValueError:
                result["errorCode"] = None
            result["timings"]["doneMs"] = ms()
            return result

        def lines() -> Iterator[str]:
            for line in response.iter_lines():
                result["timings"].setdefault("firstByteMs", ms())
                yield line

        for name, data in iter_sse(lines()):
            result["events"].append(name)
            if name == f"{prefix}.delta":
                at = ms()
                result["timings"].setdefault("firstDeltaMs", at)
                result["deltas"].append(data.get("text", ""))
                result["deltaMs"].append(at)  # arrival per delta, to replay the stream later
            elif name == f"{prefix}.tool_call":
                result["tools"].append(data)
            elif name == f"{prefix}.metrics":
                result["metrics"] = data
            elif name == f"{prefix}.done":
                result["timings"]["doneMs"] = ms()
                result["done"] = data
                result["status"] = "done"
            elif name == f"{prefix}.error":
                result["timings"]["doneMs"] = ms()
                result["status"] = "error"
                result["errorCode"] = data.get("code")
    result.setdefault("status", "incomplete")
    return result


def call_ai(
    client: httpx.Client,
    base_url: str,
    token: str,
    question: dict[str, Any],
    variant: dict[str, Any],
    user_id: int,
) -> dict[str, Any]:
    run_id = "run_" + uuid.uuid4().hex
    body = {
        "runId": run_id,
        "userId": user_id,
        "sessionId": 1,
        "language": "en",
        "history": question.get("history", []),
        "message": question["text"],
    }
    headers = {
        "X-Internal-Token": token,
        "X-Chat-Metrics": "1",
        "X-Request-Id": f"eval_{run_id[4:24]}",
    }
    header = config_header(variant)
    if header:
        headers["X-Chat-Config"] = header
    url = base_url.rstrip("/") + "/internal/v1/agent/chat/stream"
    return stream_call(client, url, body, headers, "agent")


def call_spring(
    client: httpx.Client, base_url: str, jwt: str, question: dict[str, Any]
) -> dict[str, Any]:
    """CHT-01 new session, then CHT-04. History cannot be injected here (it lives in MySQL)."""
    auth = {"Authorization": f"Bearer {jwt}"}
    base = base_url.rstrip("/")
    created = client.post(f"{base}/api/v1/chat/sessions", json={"title": None}, headers=auth)
    created.raise_for_status()
    session_id = created.json()["id"]
    body = {"clientMessageId": str(uuid.uuid4()), "content": question["text"]}
    url = f"{base}/api/v1/chat/sessions/{session_id}/messages/stream"
    return stream_call(client, url, body, {**auth, "Accept": "text/event-stream"}, "chat")


def reference_matches(answer: str, ref: dict[str, Any]) -> list[str]:
    """Answer numbers that state the reference value (number_check scan, same rounding rules).

    KRW: exact, or rounded half-up to 1,000 / 10,000 won ("about 8.1 million").
    percent: the one-decimal value or the value rounded half-up to a whole number (29.8, 30).
    """
    value = Decimal(str(ref["value"]))
    if ref["unit"] == "percent":
        accepted = {value, value.quantize(Decimal(1), rounding=ROUND_HALF_UP)}
        kinds = {"percent"}
    else:
        accepted = {value} | rounded_values({value})
        kinds = {"amount", "number"}
    return [
        text for text, found, kind, _ in scan_numbers(answer) if kind in kinds and found in accepted
    ]


def reference_check(
    answer: str, refs: list[dict[str, Any]], unsupported: list[str]
) -> dict[str, Any]:
    """reference: every reference value is in the answer; wrong_number: not, but the answer has
    numbers the tool result does not support; no_value: neither (e.g. it declined)."""
    matched = {ref["formula"]: reference_matches(answer, ref) for ref in refs}
    if all(matched.values()):
        verdict = "reference"
    elif unsupported:
        verdict = "wrong_number"
    else:
        verdict = "no_value"
    return {"verdict": verdict, "matched": matched, "unsupported": unsupported}


def score(row: dict[str, Any]) -> None:
    """Number check, reference check and expected-tool match from what the row already holds."""
    answer = row.get("answer") or ""
    tool_results = [row["toolResult"]]
    if row.get("judgmentFacts"):
        # Same function as the server, on the same summary and this turn's message.
        facts = judgment_facts(row["toolResult"], row["contextTexts"][0])
        if facts is not None:
            tool_results.append(facts)
    check = check_answer(answer, tool_results, row["contextTexts"])
    row["numberCheck"] = check.to_json()
    if row.get("referenceValues"):
        row["referenceCheck"] = reference_check(answer, row["referenceValues"], check.unsupported)
    called = sorted({t.get("toolName") for t in row.get("tools", [])})
    row["toolMatch"] = called == sorted(set(row["expectedTools"]))


def percentile_95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(math.ceil(0.95 * len(ordered)), 1) - 1]


def _stat(values: list[float]) -> str:
    if not values:
        return "-"
    text = f"{statistics.median(values):.0f} ms ({min(values):.0f}~{max(values):.0f})"
    if len(values) >= 20:
        text += f", p95 {percentile_95(values):.0f}"
    return text


def render_summary(conditions: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    out = [
        "# 챗봇 측정 요약",
        "",
        "## 측정 조건",
        "",
        f"- 대상: {conditions['target']}, 모드(--llm-mode): {conditions['mode']}",
        f"- 질문 세트: {(conditions.get('questionSet') or {}).get('file', '-')} "
        f"({(conditions.get('questionSet') or {}).get('version', '-')})",
        f"- 시작: {conditions['startedAt']}, 상태: {conditions.get('status')}",
        f"- 문항 {len(conditions['questions'])}개, 반복 {conditions['repeats']}, "
        f"설정마다 워밍업 {conditions['warmup']}",
        f"- 비용 상한 ${conditions['maxUsd']}, 쓴 비용 ${conditions.get('spentUsd', 0):.4f}",
        "- 설정: "
        + "; ".join(
            f"{v['name']}(model={v.get('model')}, thinking={v.get('thinking')}, "
            f"effort={v.get('effort') or '-'}, maxTokens={v.get('maxTokens')}, "
            f"judgmentFacts={v.get('judgmentFacts')}, promptVersion={v.get('promptVersion')})"
            for v in conditions["variants"]
        ),
    ]
    if conditions["target"] == "spring":
        out.append("- spring 대상은 지연만 기록(토큰은 ai 대상에서 잰다).")
        out.append("- spring 대상은 H 문항의 history 를 보내지 못한다(대화 기록은 MySQL 에 있음).")
    for variant in conditions["variants"]:
        mine = [r for r in rows if r["variant"] == variant["name"] and not r["warmup"]]
        if not mine:
            continue
        done = [r for r in mine if r["status"] == "done"]
        first = [r["timings"]["firstDeltaMs"] for r in done if "firstDeltaMs" in r["timings"]]
        server_first = [
            r["serverTimings"]["firstDeltaMs"]
            for r in done
            if (r.get("serverTimings") or {}).get("firstDeltaMs") is not None
        ]
        answer_first = [
            r["metrics"]["firstAnswerDeltaMs"]
            for r in done
            if (r.get("metrics") or {}).get("firstAnswerDeltaMs") is not None
        ]
        measured = [r for r in mine if r.get("metrics")]
        pre_tool = [r for r in measured if (r["metrics"].get("preToolTextChars") or 0) > 0]
        total = [r["timings"]["doneMs"] for r in done if "doneMs" in r["timings"]]
        calls = [c for r in mine for c in (r.get("metrics") or {}).get("calls", [])]
        unsupported = [r for r in done if r["numberCheck"]["unsupported"]]
        max_token_stops = sum(1 for c in calls if c.get("stopReason") == "max_tokens")
        out += [
            "",
            f"## {variant['name']}",
            "",
            f"- 호출 {len(mine)} (완료 {len(done)}, 오류 {len(mine) - len(done)})",
            f"- 첫 delta: {_stat(first)}",
            f"- 서버 firstDeltaMs(첫 text): {_stat(server_first)}",
            f"- 서버 firstAnswerDeltaMs(도구 결과 뒤 답변 호출의 첫 text): {_stat(answer_first)}",
            f"- 도구 전 text가 나온 회차: {len(pre_tool)}/{len(measured)}",
            f"- 전체: {_stat(total)}",
            f"- stop_reason=max_tokens: {max_token_stops}",
            "- 평균 thinking 블록(호출당): "
            + (
                f"{sum(c.get('thinkingBlocks') or 0 for c in calls) / len(mine):.2f}"
                if mine
                else "-"
            ),
            f"- 도구 선택 일치: {sum(1 for r in mine if r.get('toolMatch')) / len(mine):.0%}",
            f"- 근거 없는 숫자가 있는 답변: {len(unsupported)}/{len(done)}"
            + (
                " — "
                + "; ".join(
                    f"{r['questionId']}: {', '.join(r['numberCheck']['unsupported'])}"
                    for r in unsupported
                )
                if unsupported
                else ""
            ),
        ]
        calc = [r for r in done if r.get("referenceCheck")]
        if calc:
            verdicts = [r["referenceCheck"]["verdict"] for r in calc]
            out.append(
                f"- 계산 질문 {len(calc)}개: 참조값 포함 {verdicts.count('reference')} / "
                f"다른 숫자 {verdicts.count('wrong_number')} / 값 없음 {verdicts.count('no_value')}"
            )
            for r in calc:
                check = r["referenceCheck"]
                found = [t for texts in check["matched"].values() for t in texts]
                detail = {
                    "reference": f"참조값 포함 ({', '.join(found)})",
                    "wrong_number": f"다른 숫자 ({', '.join(check['unsupported'])})",
                    "no_value": "값 없음",
                }[check["verdict"]]
                out.append(f"  - {r['questionId']} #{r['repeat']}: {detail}")
        out += source_split_table(mine)
        out += [
            "",
            "| 문항 | 호출 | 입력 토큰 | 출력 토큰 | 비용(USD) |",
            "|---|---|---|---|---|",
        ]
        for qid in dict.fromkeys(r["questionId"] for r in mine):
            q_rows = [r for r in mine if r["questionId"] == qid]
            n = len(q_rows)
            usage = [r.get("usage") or {} for r in q_rows]
            out.append(
                f"| {qid} | {n} | {sum(u.get('inputTokens', 0) for u in usage) / n:.0f} | "
                f"{sum(u.get('outputTokens', 0) for u in usage) / n:.0f} | "
                f"{sum(r.get('costUsd', 0) for r in q_rows) / n:.5f} |"
            )
    out += judgment_section(conditions, rows)
    return "\n".join(out) + "\n"


HOLDOUT_SOURCE = "holdout_v1"


def source_split_table(mine: list[dict[str, Any]]) -> list[str]:
    """Existing questions vs hold-out (source holdout_v1), from saved fields only (rescore-safe).

    Counts only; the verdicts are the ones score() already made.
    """
    out = [
        "",
        "| 구분 | 판단형 채점 목록 | 계산 참조값 (포함 / 다른 숫자 / 값 없음) | "
        "근거 없는 숫자 (답변 / 숫자 수) |",
        "|---|---|---|---|",
    ]
    for label, group in (
        ("기존 문항", [r for r in mine if r.get("source") != HOLDOUT_SOURCE]),
        ("hold-out", [r for r in mine if r.get("source") == HOLDOUT_SOURCE]),
    ):
        done = [r for r in group if r["status"] == "done"]
        graded = sum(1 for r in group if r.get("expectedConclusion"))
        verdicts = [r["referenceCheck"]["verdict"] for r in done if r.get("referenceCheck")]
        unsupported = [r["numberCheck"]["unsupported"] for r in done]
        out.append(
            f"| {label} | {graded} | {len(verdicts)}개: {verdicts.count('reference')} / "
            f"{verdicts.count('wrong_number')} / {verdicts.count('no_value')} | "
            f"{sum(1 for u in unsupported if u)}/{len(done)} / {sum(len(u) for u in unsupported)} |"
        )
    return out


def _cell(text: str) -> str:
    """One markdown table cell: line breaks become spaces, pipes are escaped."""
    return " ".join(text.split()).replace("|", "\\|")


def judgment_section(conditions: dict[str, Any], rows: list[dict[str, Any]]) -> list[str]:
    """Judgment answers next to the expected conclusion, for a person to grade (no auto score)."""
    judged = [r for r in rows if r.get("expectedConclusion") and not r["warmup"]]
    if not judged:
        return []
    out = ["", "## 판단형 문항(사람 채점)", ""]
    for variant in conditions["variants"]:
        mine = [r for r in judged if r["variant"] == variant["name"]]
        if not mine:
            continue
        out += [
            f"### {variant['name']}",
            "",
            "| 문항 | 회차 | 기대 결론 | 답변 |",
            "|---|---|---|---|",
        ]
        for r in mine:
            answer = r.get("answer") or ""
            if r["status"] != "done":
                answer = f"(오류 {r.get('errorCode') or r['status']}) {answer}"
            out.append(
                f"| {r['questionId']} | {r['repeat']} | {_cell(r['expectedConclusion'])} | "
                f"{_cell(answer)} |"
            )
        out.append("")
    return out[:-1]


def write_jsonl(path: Path, conditions: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "conditions", **conditions}, ensure_ascii=False) + "\n")
        for row in rows:
            fh.write(json.dumps({"type": "call", **row}, ensure_ascii=False) + "\n")
    tmp.replace(path)
    path.with_suffix(".md").write_text(render_summary(conditions, rows), encoding="utf-8")


def read_jsonl(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    conditions: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        kind = item.pop("type", None)
        if kind == "conditions":
            conditions = item
        elif kind == "call":
            rows.append(item)
    return conditions, rows


def progress_line(done: int, total: int, row: dict[str, Any], spent: float) -> str:
    t = row["timings"]
    return (
        f"[{done}/{total}] {row['variant']} {row['questionId']} #{row['repeat']}"
        f"{' (warmup)' if row['warmup'] else ''} {row['status']}"
        f"{':' + str(row.get('errorCode')) if row.get('errorCode') else ''} "
        f"first={t.get('firstDeltaMs', '-')}ms done={t.get('doneMs', '-')}ms "
        f"in={(row.get('usage') or {}).get('inputTokens', '-')} "
        f"out={(row.get('usage') or {}).get('outputTokens', '-')} "
        f"${row.get('costUsd', 0):.4f} total=${spent:.4f}"
    )


def main(
    argv: list[str] | None = None,
    *,
    client: httpx.Client | None = None,
    settings: Settings | None = None,
    confirm: Callable[[str], str] = input,
) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--target", choices=["ai", "spring"], default="ai")
    parser.add_argument(
        "--llm-mode",
        choices=["fake", "anthropic"],
        default="fake",
        help="what the server runs; anthropic asks before paid calls (not read from env)",
    )
    parser.add_argument("--base-url", default=None, help="ai: http://localhost:8000, spring: :8080")
    parser.add_argument("--jwt", default=None, help="spring target: access token from /auth/entry")
    parser.add_argument("--user-id", type=int, default=1, help="ai target userId")
    parser.add_argument("--variants", type=Path, default=None)
    parser.add_argument("--questions", default=None, help="comma-separated ids, e.g. F01,F03")
    parser.add_argument("--subset", choices=["latency"], default=None)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--warmup", type=int, default=0, help="calls per variant, not summarized")
    parser.add_argument("--output", type=Path, default=None, help="JSONL path (required)")
    parser.add_argument("--max-usd", type=float, default=1.0)
    parser.add_argument("--rescore", type=Path, default=None, help="re-score a saved JSONL")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args(argv)

    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.output is None:
        print("REFUSED: --output is required (results are saved after every call)")
        return EXIT_USAGE

    if args.rescore is not None:
        saved_conditions, saved_rows = read_jsonl(args.rescore)
        for saved in saved_rows:
            score(saved)
        saved_conditions["rescoredFrom"] = str(args.rescore)
        write_jsonl(args.output, saved_conditions, saved_rows)
        print(f"rescored {len(saved_rows)} calls -> {args.output} (+ .md)")
        return EXIT_OK

    settings = settings or get_settings()
    if args.target == "spring" and not args.jwt:
        parser.error("--target spring needs --jwt")
    if args.target == "spring" and args.variants:
        parser.error(
            "--variants works with --target ai only (Spring does not forward X-Chat-Config)"
        )
    if args.repeats < 1 or args.warmup < 0:
        parser.error("--repeats must be >= 1 and --warmup >= 0")
    if args.target == "ai" and not settings.internal_token_configured():
        print("BLOCKED: AI_INTERNAL_API_TOKEN is not configured")
        return EXIT_BLOCKED
    spec = load_questions()
    questions = select_questions(spec, args.questions, args.subset, parser)
    try:
        variants = load_variants(args.variants, settings)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    fake = args.llm_mode == "fake"
    base_url = args.base_url or (
        "http://localhost:8000" if args.target == "ai" else "http://localhost:8080"
    )

    total_calls = len(variants) * (args.warmup + len(questions) * args.repeats)
    estimates = {v["name"]: estimate_call_usd(v["model"], v["maxTokens"], fake) for v in variants}
    planned_max = sum(
        estimates[v["name"]] * (args.warmup + len(questions) * args.repeats) for v in variants
    )
    print(f"llm-mode={args.llm_mode} target={args.target} base={base_url}")
    for v in variants:
        print(
            f"  {v['name']}: model={v['model']} thinking={v.get('thinking')} "
            f"effort={v.get('effort') or '-'} maxTokens={v['maxTokens']} "
            f"judgmentFacts={v.get('judgmentFacts')} promptVersion={v.get('promptVersion')} "
            f"(<= ${estimates[v['name']]:.4f}/call)"
        )
    print(
        f"총 {total_calls}회 호출(설정 {len(variants)} × (워밍업 {args.warmup} + 문항 "
        f"{len(questions)} × 반복 {args.repeats})), 예상 최대 ${planned_max:.4f} "
        f"(호출당 LLM {EST_LLM_CALLS}회·출력 maxTokens 가정), 상한 ${args.max_usd}",
        flush=True,
    )
    if not fake and not args.yes and confirm("계속할까요? [y/N] ").strip().lower() != "y":
        print("cancelled")
        return EXIT_OK

    context = tool_context(settings, args.user_id, args.llm_mode)
    conditions: dict[str, Any] = {
        "target": args.target,
        "mode": args.llm_mode,
        "baseUrl": base_url,
        "userId": args.user_id,
        "variants": [{k: v for k, v in variant.items() if k != "override"} for variant in variants],
        "questionSet": {"file": QUESTIONS.name, "version": spec.get("version")},
        "questions": [q["id"] for q in questions],
        "repeats": args.repeats,
        "warmup": args.warmup,
        "maxUsd": args.max_usd,
        "startedAt": datetime.now(UTC).isoformat(),
        "status": "running",
        "spentUsd": 0.0,
    }
    rows: list[dict[str, Any]] = []
    observed: dict[str, float] = {}
    owns_client = client is None
    http = client or httpx.Client()
    token = settings.ai_internal_api_token.get_secret_value()

    schedule: list[tuple[dict[str, Any], dict[str, Any], int, bool]] = []
    for variant in variants:
        for i in range(args.warmup):
            schedule.append((variant, questions[i % len(questions)], i + 1, True))
    for question in questions:
        for repeat in range(1, args.repeats + 1):
            for variant in variants:  # round-robin inside the same question and repeat
                schedule.append((variant, question, repeat, False))

    def save(status: str) -> None:
        conditions["status"] = status
        conditions["spentUsd"] = round(sum(r.get("costUsd", 0) for r in rows), 6)
        write_jsonl(args.output, conditions, rows)

    try:
        for variant, question, repeat, warmup in schedule:
            spent = sum(r.get("costUsd", 0) for r in rows)
            next_cost = observed.get(variant["name"], estimates[variant["name"]])
            if spent + next_cost > args.max_usd:
                print(f"budget stop: spent ${spent:.4f} + next ~${next_cost:.4f} > ${args.max_usd}")
                save("budget_stopped")
                print(render_summary(conditions, rows))
                return EXIT_OK
            if args.target == "ai":
                result = call_ai(http, base_url, token, question, variant, args.user_id)
            else:
                result = call_spring(http, base_url, args.jwt, question)
            done = result.get("done") or {}
            metrics = result.get("metrics")
            answered_by = answering_model(done, metrics)
            model = answered_by or (metrics or {}).get("config", {}).get("model")
            usage = run_usage(done, metrics)
            row: dict[str, Any] = {
                "variant": variant["name"],
                "questionId": question["id"],
                "category": question["category"],
                "repeat": repeat,
                "warmup": warmup,
                "at": datetime.now(UTC).isoformat(),
                "status": result.get("status"),
                "httpStatus": result.get("httpStatus"),
                "errorCode": result.get("errorCode"),
                "events": result["events"],
                "timings": result["timings"],
                "serverTimings": done.get("timings") or {"latencyMs": done.get("latencyMs")},
                "usage": usage,
                "metrics": metrics,
                "model": model,
                "costUsd": round(cost_usd(usage, model), 6),
                "tools": result["tools"],
                "answer": "".join(result["deltas"]),
                "deltaTimesMs": result["deltaMs"],
                "expectedTools": question["expectedTools"],
                "referenceValues": question.get("referenceValues"),
                # Listed for a person to grade: judgment questions and humanGraded ones only
                # (other calculation questions keep their conclusion in the spec, not here).
                "expectedConclusion": (
                    question.get("expectedConclusion")
                    if question["category"] == "judgment" or question.get("humanGraded")
                    else None
                ),
                "source": question.get("source"),
                "judgmentFacts": bool(variant.get("judgmentFacts")),
                "toolResult": context,
                "contextTexts": [
                    question["text"],
                    *(h["content"] for h in question.get("history", [])),
                ],
            }
            score(row)
            rows.append(row)
            observed[variant["name"]] = max(observed.get(variant["name"], 0.0), row["costUsd"])
            save("running")
            print(progress_line(len(rows), total_calls, row, conditions["spentUsd"]), flush=True)
            used_tokens = any((usage or {}).get(k) for k in USAGE_KEYS)
            if fake and (answered_by not in (None, FAKE_MODEL_ID) or used_tokens):
                print(
                    "STOP: 서버가 실제 모드입니다(--llm-mode fake인데 응답에 실제 모델이나 "
                    "토큰 사용량이 있음). 서버를 fake로 띄우거나 --llm-mode anthropic으로 "
                    "다시 실행하세요."
                )
                save("mode_mismatch")
                return EXIT_BLOCKED
    except KeyboardInterrupt:
        save("interrupted")
        print(f"interrupted after {len(rows)}/{total_calls} calls")
        return EXIT_INTERRUPTED
    finally:
        if owns_client:
            http.close()

    save("completed")
    print(render_summary(conditions, rows))
    print(json.dumps({"output": str(args.output), "summary": str(args.output.with_suffix(".md"))}))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

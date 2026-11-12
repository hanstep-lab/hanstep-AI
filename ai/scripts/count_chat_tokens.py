"""Input tokens per finance question: raw transactions in the prompt vs. the tool approach.

Uses the Anthropic count_tokens API (free, no generation). count_tokens is an estimate.
Not part of pytest or CI; run it yourself:

    uv run python scripts/count_chat_tokens.py --output artifacts/chat/token_count.md

For each finance question (category finance in specs/chat/eval_questions_v1.json):
1. raw: short system + the 341 Minh transactions (tests/fixtures/finance/minh_12m.json) +
   question, no tools.
2. tool: chat system prompt v1 + tool definition + question (first call), plus the second call
   with the tool_use and the tool result (chat fixture summary). Reported as the sum.
Also prints the prefix alone (tools + system) and whether it reaches the 512-token minimum
for prompt caching. The API key is never printed or written.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401

from app.chat.prompts import PROMPT_VERSION, system_prompt
from app.chat.tools import FIXTURE, TOOL_NAME, TOOL_SCHEMA, summarize
from app.core.config import get_settings
from app.rag.cli import EXIT_BLOCKED, EXIT_OK

AI_ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = AI_ROOT / "specs" / "chat" / "eval_questions_v1.json"
TRANSACTIONS = AI_ROOT / "tests" / "fixtures" / "finance" / "minh_12m.json"
CACHE_MIN_TOKENS = 512
EXIT_USAGE = 2
RAW_SYSTEM = (
    "You are a financial assistant. Answer the user's question about their money using only "
    "the transactions given. Amounts are KRW."
)


def finance_questions() -> list[dict[str, Any]]:
    spec = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    return [q for q in spec["questions"] if q["category"] == "finance"]


def raw_request(question: str) -> dict[str, Any]:
    request = json.loads(TRANSACTIONS.read_text(encoding="utf-8"))["request"]
    transactions = json.dumps(request["transactions"], ensure_ascii=False)
    return {
        "system": RAW_SYSTEM,
        "messages": [
            {
                "role": "user",
                "content": f"<transactions>\n{transactions}\n</transactions>\n\n{question}",
            }
        ],
    }


def tool_requests(question: str) -> list[dict[str, Any]]:
    system = system_prompt("en")
    result = summarize(json.loads(FIXTURE.read_text(encoding="utf-8")))
    first = [{"role": "user", "content": question}]
    second = [
        *first,
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_count_1", "name": TOOL_NAME, "input": {}}
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_count_1",
                    "content": json.dumps(result, ensure_ascii=False),
                }
            ],
        },
    ]
    return [
        {"system": system, "tools": [TOOL_SCHEMA], "messages": first},
        {"system": system, "tools": [TOOL_SCHEMA], "messages": second},
    ]


def count(client: Any, model: str, request: dict[str, Any]) -> int:
    response = client.messages.count_tokens(model=model, **request)
    return int(response.input_tokens)


def prefix_tokens(client: Any, model: str) -> int:
    """tools + system alone: the same tiny message with and without the prefix."""
    message = [{"role": "user", "content": "x"}]
    with_prefix = count(
        client, model, {"system": system_prompt("en"), "tools": [TOOL_SCHEMA], "messages": message}
    )
    return with_prefix - count(client, model, {"messages": message})


def render(rows: list[dict[str, Any]], prefixes: dict[str, int]) -> str:
    out = [
        "# 챗봇 입력 토큰 비교 (count_tokens)",
        "",
        "count_tokens 는 추정치다. 실제 과금 토큰과 조금 다를 수 있다.",
        f"도구 방식 system 프롬프트: {PROMPT_VERSION}. 거래 원문: minh_12m.json 거래 341건.",
        "",
        "## 앞부분(tools + system)",
        "",
        "| 모델 | 토큰 | 캐싱 최소(512) 이상 |",
        "|---|---|---|",
    ]
    for model, tokens in prefixes.items():
        out.append(f"| {model} | {tokens} | {'예' if tokens >= CACHE_MIN_TOKENS else '아니오'} |")
    out += [
        "",
        "## 문항별 입력 토큰",
        "",
        "| 모델 | 문항 | ① 거래 원문 | ② 도구 1차 | ② 도구 2차 | ② 합계 | ②/① |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        total = r["tool_first"] + r["tool_second"]
        out.append(
            f"| {r['model']} | {r['question']} | {r['raw']} | {r['tool_first']} | "
            f"{r['tool_second']} | {total} | {total / r['raw']:.2f} |"
        )
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None, *, client: Any = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--models",
        default="claude-sonnet-5-5,claude-haiku-5-5",
        help="comma-separated model ids",
    )
    parser.add_argument("--output", type=Path, default=None, help="markdown path (required)")
    args = parser.parse_args(argv)
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.output is None:
        print("REFUSED: --output is required")
        return EXIT_USAGE
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if not models:
        parser.error("--models is empty")

    if client is None:
        settings = get_settings()
        if not settings.anthropic_configured():
            print("BLOCKED: ANTHROPIC_API_KEY is not configured")
            return EXIT_BLOCKED
        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key.get_secret_value())

    questions = finance_questions()
    rows: list[dict[str, Any]] = []
    prefixes: dict[str, int] = {}
    for model in models:
        prefixes[model] = prefix_tokens(client, model)
        for q in questions:
            first, second = tool_requests(q["text"])
            row = {
                "model": model,
                "question": q["id"],
                "raw": count(client, model, raw_request(q["text"])),
                "tool_first": count(client, model, first),
                "tool_second": count(client, model, second),
            }
            rows.append(row)
            print(
                f"{model} {q['id']} raw={row['raw']} tool={row['tool_first']}+{row['tool_second']}",
                flush=True,
            )
    report = render(rows, prefixes)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(report)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

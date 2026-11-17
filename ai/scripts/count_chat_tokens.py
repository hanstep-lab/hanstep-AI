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

--question-set product (issue #65 baseline): each question in specs/chat/product_questions_v1.json
with the current chat setup (system prompt v3 + the v3 tools: finance, visa, remittance, no product
tool). Counts the first call, and the second call as if get_financial_analysis was called with
the chat fixture (the baseline may call it for spare-money questions). History is included.

    uv run python scripts/count_chat_tokens.py --question-set product \
        --models claude-haiku-5-5 --output artifacts/chat/product_baseline_tokens.md

--question-set policy (issue #88 baseline): each question in
specs/rag/finance_policy_questions_v1.json with the current default chat (system prompt
v4.1 + its tools: finance, visa, remittance, product; no regulation tool).
Same first/second call as product.

    uv run python scripts/count_chat_tokens.py --question-set policy \
        --models claude-haiku-5-5 --output artifacts/chat/policy_baseline_tokens.md
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401

from app.chat.prompts import PROMPT_VERSION, PROMPT_VERSIONS, system_prompt
from app.chat.remittance_tools import TOOL_SCHEMAS as REMITTANCE_TOOL_SCHEMAS
from app.chat.tools import FIXTURE, TOOL_NAME, TOOL_SCHEMA, summarize
from app.chat.visa_tools import VISA_TOOL_NAMES
from app.core.config import get_settings
from app.rag.cli import EXIT_BLOCKED, EXIT_OK

AI_ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = AI_ROOT / "specs" / "chat" / "eval_questions_v1.json"
PRODUCT_QUESTIONS = AI_ROOT / "specs" / "chat" / "product_questions_v1.json"
POLICY_QUESTIONS = AI_ROOT / "specs" / "rag" / "finance_policy_questions_v1.json"
BASELINE_PROMPT = "v3"
# 금융 정책 RAG(#88) 기준선: 지금 기본 프롬프트(규정 도구 없음).
POLICY_BASELINE_PROMPT = "v4.1"
# 금융 정책 검색 도구가 붙는 버전(#88 after).
# 2차 호출은 검색 결과 예시(fixture)를 받았다고 가정한다.
POLICY_TOOL_PROMPT = "v4.2"
# 스펙 가드 비교안 ①(#88): v4.2 + 수령·지급 방향 규칙 한 줄. 도구 구성은 v4.2와 같다.
POLICY_GUARD_PROMPT = "v4.3a"
POLICY_RESULT_SAMPLE = AI_ROOT / "tests" / "fixtures" / "chat" / "finance_policy_result_sample.json"
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


def v3_tools() -> list[dict[str, Any]]:
    """The tools the server binds for prompt v3 (finance + visa + remittance), in that order."""
    from app.visa.tools import tool_definitions

    visa = [d for d in tool_definitions() if d["name"] in VISA_TOOL_NAMES]
    return [TOOL_SCHEMA, *visa, *(dict(s) for s in REMITTANCE_TOOL_SCHEMAS)]


def v4_1_tools() -> list[dict[str, Any]]:
    """The tools the server binds for prompt v4.1: the v3 tools + the product tool (v4.1 schema)."""
    from app.chat.product_tools import TOOL_SCHEMAS_BY_PROMPT

    return [*v3_tools(), TOOL_SCHEMAS_BY_PROMPT[POLICY_BASELINE_PROMPT]]


def v4_2_tools() -> list[dict[str, Any]]:
    """v4.2: v4.1 도구 + 금융 정책 검색 도구."""
    from app.chat.finance_policy_tools import TOOL_SCHEMA as POLICY_TOOL_SCHEMA

    return [*v4_1_tools(), POLICY_TOOL_SCHEMA]


def history_messages(history: list[dict[str, str]]) -> list[dict[str, Any]]:
    return [{"role": h["role"].lower(), "content": h["content"]} for h in history]


def baseline_requests(
    question: dict[str, Any],
    prompt: str = BASELINE_PROMPT,
    tools: list[dict[str, Any]] | None = None,
    second_tool: tuple[str, dict[str, Any], Any] | None = None,
) -> list[dict[str, Any]]:
    """Question with a chat setup (default: v3 + v3 tools): first and second call.

    second_tool: (도구 이름, 인자, 결과). 없으면 get_financial_analysis + fixture 요약.
    """
    system = system_prompt("en", prompt)
    tools = v3_tools() if tools is None else tools
    name, arguments, result = second_tool or (
        TOOL_NAME,
        {},
        summarize(json.loads(FIXTURE.read_text(encoding="utf-8"))),
    )
    first = [*history_messages(question["history"]), {"role": "user", "content": question["text"]}]
    second = [
        *first,
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_count_1", "name": name, "input": arguments}
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
        {"system": system, "tools": tools, "messages": first},
        {"system": system, "tools": tools, "messages": second},
    ]


def render_baseline(rows: list[dict[str, Any]], setup: str | None = None) -> str:
    setup = setup or (
        f"지금 챗봇 설정: system {PROMPT_VERSIONS[BASELINE_PROMPT]} + v3 도구(금융·비자·송금), "
        "상품 도구 없음. 문항: product_questions_v1.json."
    )
    out = [
        "# 질문 기준선 입력 토큰 (count_tokens)",
        "",
        "count_tokens 는 추정치다. 실제 과금 토큰과 조금 다를 수 있다.",
        setup,
        "2차는 get_financial_analysis를 부르고 fixture 결과를 받았다고 가정한 호출이다.",
        "",
        "| 모델 | 문항 | 1차 | 2차 | 합계 |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        total = r["first"] + r["second"]
        out.append(f"| {r['model']} | {r['question']} | {r['first']} | {r['second']} | {total} |")
    for model in dict.fromkeys(r["model"] for r in rows):
        mine = [r for r in rows if r["model"] == model]
        firsts = [r["first"] for r in mine]
        totals = [r["first"] + r["second"] for r in mine]
        out.append(
            f"| {model} | 평균 | {sum(firsts) / len(firsts):.0f} | – | "
            f"{sum(totals) / len(totals):.0f} |"
        )
    return "\n".join(out) + "\n"


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
    parser.add_argument(
        "--question-set",
        choices=["finance", "product", "policy"],
        default="finance",
        help="finance: raw vs tool (eval_questions_v1.json), product: #65 baseline (v3 tools), "
        "policy: #88 baseline (v4.1 tools)",
    )
    parser.add_argument(
        "--prompt-version",
        choices=[POLICY_BASELINE_PROMPT, POLICY_TOOL_PROMPT, POLICY_GUARD_PROMPT],
        default=POLICY_BASELINE_PROMPT,
        help="policy only: v4.1 baseline (no regulation tool), v4.2 (search_finance_policy) or "
        "v4.3a (v4.2 + direction rule)",
    )
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

    if args.question_set in ("product", "policy"):
        policy = args.question_set == "policy"
        spec = json.loads((POLICY_QUESTIONS if policy else PRODUCT_QUESTIONS).read_text("utf-8"))
        with_tool = policy and args.prompt_version in (POLICY_TOOL_PROMPT, POLICY_GUARD_PROMPT)
        prompt = args.prompt_version if policy else BASELINE_PROMPT
        tools = v4_2_tools() if with_tool else v4_1_tools() if policy else v3_tools()
        second_tool = None
        if with_tool:
            from app.chat.finance_policy_tools import SEARCH_FINANCE_POLICY

            sample = json.loads(POLICY_RESULT_SAMPLE.read_text(encoding="utf-8"))
            second_tool = (SEARCH_FINANCE_POLICY, {"queryKo": sample["query"]}, sample["result"])
        base_rows: list[dict[str, Any]] = []
        for model in models:
            for q in spec["questions"]:
                first, second = baseline_requests(q, prompt, tools, second_tool)
                row = {
                    "model": model,
                    "question": q["id"],
                    "first": count(client, model, first),
                    "second": count(client, model, second),
                }
                base_rows.append(row)
                print(f"{model} {q['id']} first={row['first']} second={row['second']}", flush=True)
        setup = None
        if with_tool:
            setup = (
                f"챗봇 설정: system {PROMPT_VERSIONS[args.prompt_version]} + v4.1 도구 + "
                "search_finance_policy. 문항: finance_policy_questions_v1.json. "
                "아래 2차는 search_finance_policy 결과 예시(k=8)를 받았다고 가정한 호출이다."
            )
        elif policy:
            setup = (
                f"지금 챗봇 설정: system {PROMPT_VERSIONS[POLICY_BASELINE_PROMPT]} + v4.1 도구"
                "(금융·비자·송금·상품), 규정 도구 없음. 문항: finance_policy_questions_v1.json."
            )
        report = render_baseline(base_rows, setup)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
        print(report)
        return EXIT_OK

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

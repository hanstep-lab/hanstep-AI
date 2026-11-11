"""LLM 직접 계산과 INT-01 규칙 엔진 비교: 정확도, 일관성, 토큰, 지연 시간.

pytest·CI에 포함되지 않는다. 케이스는 ready 상태이면서 400이 아닌 시나리오와 Minh 12개월
목업이다. LLM은 같은 계산 규칙(2-7)과 거래 JSON을 도구 없이 받고, 정수로 된 JSON 객체로
답해야 한다. API 키는 절대 출력하거나 파일에 쓰지 않는다.

    uv run python scripts/evaluate_finance_llm.py --repeats 3 \
        --output artifacts/finance/llm_comparison.json
    uv run python scripts/evaluate_finance_llm.py --repeats 1 --cases S02,S08

호출마다 진행 상황을 한 줄 출력하고 호출이 끝날 때마다 --output을 다시 쓴다("status":
running -> completed, Ctrl+C면 interrupted). 그래서 중간에 멈춰도 비용을 낸 결과는 남는다.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401

from app.core.config import get_settings
from app.finance.calculator import analyze
from app.finance.schemas import FinanceAnalyzeRequest
from app.rag.cli import EXIT_BLOCKED, EXIT_OK, print_json, write_json

AI_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = AI_ROOT / "tests" / "fixtures" / "finance"
FIELDS = ["avgMonthlyIncome", "fixedLiving", "remittance", "variableAvg", "spare"]
MAX_TOKENS = 16000
EXIT_INTERRUPTED = 130

# INT-01 명세의 규칙 2-7을 그대로 옮겼다. 줄 끝 백슬래시는 린터 때문에 긴 소스 줄을
# 나눌 뿐이고, 프롬프트 텍스트는 바뀌지 않는다.
RULES = """\
2. 중복: 같은 id에 같은 내용이 여러 번 오면 한 번만 집계하고, \
나머지는 excluded에 reason "DUPLICATE_ID".
   id가 다르면 날짜·금액이 같아도 별개 거래로 둘 것.
3. 제외(excluded에 이유와 함께 기록, 어떤 합계에도 넣지 않음):
   transactionKind가 CARD_SETTLEMENT, OWN_TRANSFER, SAVINGS_TRANSFER, \
LOAN_DISBURSEMENT이면 이유는 그 kind 값.
   categoryCode가 NEUTRAL이면 이유는 NEUTRAL.
4. 분류(제외되지 않은 거래):
   - INCOME → 소득 (카테고리 무관)
   - EXPENSE + HOUSING/UTILITY/TELECOM → 고정지출
   - EXPENSE + transactionKind LOAN_REPAYMENT → 고정지출
   - EXPENSE + REMITTANCE → 송금
   - EXPENSE + FOOD/TRANSPORT/LIVING → 변동지출
   - EXPENSE + 그 밖의 카테고리 → 변동지출, warnings에 "UNKNOWN_CATEGORY:{코드}"를 코드마다 한 번
5. 달 구분: periodFrom~periodTo가 1일~말일을 다 덮는 달만 꽉 찬 달. \
일부만 덮는 달은 partialMonths에 넣고 평균에서 제외.
   기간은 Spring이 적재에 성공한 범위이므로 꽉 찬 달인데 거래가 0건이면 0원으로 평균에 포함.
   missingMonths는 현재 항상 빈 배열(필드만 유지).
   observedMonthCount = 꽉 찬 달 수. 0이면 금액 필드를 모두 null로 두고 \
warnings에 "NO_FULL_MONTH" (200 응답).
6. 금액: 정수(int64)로만 더함. pandas mean() 같은 float 평균 금지.
   평균 = 꽉 찬 달들의 월별 합계 총합 ÷ observedMonthCount, \
마지막에 한 번 HALF_UP 반올림(정수 연산으로 구현).
   variableExpense.min/max = 꽉 찬 달의 월별 변동지출 합계 중 최소/최대.
7. spare = 반올림된 avgMonthlyIncome − fixedLiving − remittance − variableExpense.avg. 음수 허용."""

PROMPT_TEMPLATE = """\
아래 계산 규칙에 따라 한 사용자의 거래 내역에서 월 평균 금융 밸런스를 계산해 주세요.

<rules>
{rules}
</rules>

<request>
{request}
</request>

답은 아래 형식의 JSON 객체 하나만 출력하세요. 다른 설명이나 코드 블록 없이 JSON만 출력합니다.
값은 원 단위 정수이고, 규칙 5에 따라 금액을 null로 두는 경우에만 null을 씁니다.
variableAvg는 variableExpense.avg 값입니다.
{{"avgMonthlyIncome": 정수, "fixedLiving": 정수, "remittance": 정수, \
"variableAvg": 정수, "spare": 정수}}"""


def load_cases() -> list[dict[str, Any]]:
    cases = []
    for path in sorted((FIXTURES / "scenarios").glob("S*.json")):
        scenario = json.loads(path.read_text(encoding="utf-8"))
        if scenario.get("status") != "ready" or scenario["expected"].get("status") == 400:
            continue
        cases.append({"id": scenario["id"], "request": scenario["request"]})
    minh = json.loads((FIXTURES / "minh_12m.json").read_text(encoding="utf-8"))
    cases.append({"id": "minh_12m", "request": minh["request"]})
    return cases


def engine_answer(request: dict[str, Any]) -> dict[str, int | None]:
    result = analyze(FinanceAnalyzeRequest.model_validate(request)).model_dump(
        by_alias=True, mode="json"
    )

    def amount(money: dict[str, Any] | None) -> int | None:
        return None if money is None else int(money["amount"])

    variable = result["variableExpense"]
    return {
        "avgMonthlyIncome": amount(result["avgMonthlyIncome"]),
        "fixedLiving": amount(result["fixedLiving"]),
        "remittance": amount(result["remittance"]),
        "variableAvg": None if variable is None else amount(variable["avg"]),
        "spare": amount(result["spare"]),
    }


def build_prompt(request: dict[str, Any]) -> str:
    return PROMPT_TEMPLATE.format(
        rules=RULES, request=json.dumps(request, ensure_ascii=False, indent=1)
    )


def parse_answer(text: str) -> dict[str, int | None] | None:
    """엄격한 파싱: 정확히 다섯 필드를 가진 JSON 객체 하나, 각 값은 int 또는 null."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or set(data) != set(FIELDS):
        return None
    answer: dict[str, int | None] = {}
    for key in FIELDS:
        value = data[key]
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
            return None
        answer[key] = value
    return answer


def select_cases(
    cases: list[dict[str, Any]], selection: str | None, parser: argparse.ArgumentParser
) -> list[dict[str, Any]]:
    """--cases S02,S08이면 그 id만 남기고(fixture 순서 유지), 옵션이 없으면 전부 남긴다."""
    if selection is None:
        return cases
    wanted = {part.strip() for part in selection.split(",") if part.strip()}
    known = [c["id"] for c in cases]
    unknown = sorted(wanted - set(known))
    if not wanted or unknown:
        parser.error(f"unknown or empty --cases: {unknown}; known ids: {', '.join(known)}")
    return [c for c in cases if c["id"] in wanted]


def save_report(path: Path, report: dict[str, Any]) -> None:
    # 쓴 다음 이름을 바꾼다. 저장 도중 Ctrl+C를 눌러도 반쯤 쓴 파일이 남지 않는다.
    tmp = path.with_name(path.name + ".tmp")
    write_json(tmp, report)
    tmp.replace(path)


def progress_line(done: int, total: int, row: dict[str, Any], elapsed: float) -> str:
    if row.get("error"):
        status = f"error:{row['error']}"
    elif row.get("parse_failed"):
        status = "parse_failed"
    else:
        status = "ok"
    return (
        f"[{done}/{total}] {row['case']} #{row['repeat']} done {elapsed:.1f}s "
        f"stop={row.get('stop_reason', '-')} in={row.get('input_tokens', '-')} "
        f"out={row.get('output_tokens', '-')} {status}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument(
        "--output", type=Path, default=AI_ROOT / "artifacts" / "finance" / "llm_comparison.json"
    )
    parser.add_argument("--model", default=None, help="defaults to AGENT_MODEL")
    parser.add_argument(
        "--thinking",
        choices=["adaptive", "off"],
        default="adaptive",
        help="adaptive thinking gives the LLM its best shot at the arithmetic",
    )
    parser.add_argument(
        "--cases", default=None, help="comma-separated case ids, e.g. minh_12m or S02,S08"
    )
    args = parser.parse_args()
    cases = select_cases(load_cases(), args.cases, parser)

    settings = get_settings()
    if not settings.anthropic_configured():
        print("BLOCKED: ANTHROPIC_API_KEY is not configured")
        return EXIT_BLOCKED

    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key.get_secret_value())
    model = args.model or settings.agent_model
    started_at = datetime.now(UTC).isoformat()
    conditions = {
        "model": model,
        # SDK가 보내는 API 버전 2026-10-21은 샘플링 파라미터를 거부한다. 그래서
        # temperature는 보내지 않고 API 기본 샘플링을 쓴다.
        "temperature": None,
        "temperature_note": "not sent (API default sampling)",
        "thinking": args.thinking,
        "max_tokens": MAX_TOKENS,
        "repeats": args.repeats,
        "cases": len(cases),
        "started_at": started_at,
        "sdk_version": anthropic.__version__,
    }

    rows: list[dict[str, Any]] = []
    # 중간 저장에서도 선택한 케이스를 모두 요약할 수 있게 미리 계산한다.
    prompts = {case["id"]: build_prompt(case["request"]) for case in cases}
    engine = {case["id"]: engine_answer(case["request"]) for case in cases}
    total_calls = len(cases) * args.repeats

    def save(status: str) -> dict[str, Any]:
        summary = summarize(rows, engine, cases)
        report = {
            "status": status,
            "conditions": conditions,
            "summary": summary,
            "prompts": prompts,
            "rows": rows,
        }
        save_report(args.output, report)
        return summary

    # Windows에서 파이프나 리디렉션된 stdout은 기본값이 cp1252라 한국어를 출력하지 못한다.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    print(
        f"총 {total_calls}회 호출 예정(케이스 {len(cases)}개 × 반복 {args.repeats}회), "
        f"thinking={args.thinking}, max_tokens={MAX_TOKENS}",
        flush=True,
    )
    try:
        for case in cases:
            prompt = prompts[case["id"]]
            for repeat in range(1, args.repeats + 1):
                row: dict[str, Any] = {
                    "case": case["id"],
                    "repeat": repeat,
                    "model": model,
                    "at": datetime.now(UTC).isoformat(),
                }
                request_kwargs: dict[str, Any] = {
                    "model": model,
                    "max_tokens": MAX_TOKENS,
                    "messages": [{"role": "user", "content": prompt}],
                }
                if args.thinking == "adaptive":
                    request_kwargs["thinking"] = {"type": "adaptive"}
                start = time.perf_counter()
                try:
                    response = client.messages.create(**request_kwargs)
                except anthropic.AuthenticationError:
                    print("BLOCKED: Anthropic API rejected the configured key")
                    return EXIT_BLOCKED
                except anthropic.APIStatusError as exc:
                    row.update(error=f"{type(exc).__name__}:{exc.status_code}", parse_failed=True)
                except anthropic.APIConnectionError as exc:
                    row.update(error=type(exc).__name__, parse_failed=True)
                else:
                    row["latency_ms"] = round((time.perf_counter() - start) * 1000)
                    row["input_tokens"] = response.usage.input_tokens
                    row["output_tokens"] = response.usage.output_tokens
                    row["stop_reason"] = response.stop_reason
                    text = "".join(block.text for block in response.content if block.type == "text")
                    row["raw_text"] = text
                    answer = parse_answer(text)
                    row["parse_failed"] = answer is None
                    row["answer"] = answer
                    if answer is not None:
                        expected = engine[case["id"]]
                        row["field_match"] = {k: answer[k] == expected[k] for k in FIELDS}
                        got, want = answer["spare"], expected["spare"]
                        row["spare_abs_error"] = (
                            abs(got - want) if got is not None and want is not None else None
                        )
                rows.append(row)
                save("running")
                print(
                    progress_line(len(rows), total_calls, row, time.perf_counter() - start),
                    flush=True,
                )
    except KeyboardInterrupt:
        summary = save("interrupted")
        print(f"interrupted after {len(rows)}/{total_calls} calls", flush=True)
        print_table(summary, conditions)
        print_json({"output": str(args.output), "status": "interrupted"})
        return EXIT_INTERRUPTED

    summary = save("completed")
    print_table(summary, conditions)
    print_json({"output": str(args.output)})
    return EXIT_OK


def summarize(
    rows: list[dict[str, Any]], engine: dict[str, dict[str, int | None]], cases: list[Any]
) -> dict[str, Any]:
    total = len(rows)
    parsed = [r for r in rows if not r.get("parse_failed")]
    # 정확도 분모: 모델이 실제로 답한 호출. API 오류(과부하, 네트워크)는 오답이 아니라
    # 인프라 실패다. 파싱 실패와 max_tokens 잘림은 그대로 오답으로 센다.
    scored = sum(1 for r in rows if not r.get("error"))
    field_rate = {
        k: (sum(r["field_match"][k] for r in parsed) / scored if scored else 0.0) for k in FIELDS
    }
    all_fields = sum(all(r["field_match"].values()) for r in parsed) / scored if scored else 0.0
    errors = [r["spare_abs_error"] for r in parsed if r.get("spare_abs_error") is not None]
    # 쓸 수 있는 답이 없는 호출. 서로 겹치지 않는 원인으로 나누며, 모두 더하면 total이 된다.
    failed = [r for r in rows if r.get("parse_failed")]
    api_errors = sum(1 for r in failed if r.get("error"))
    cut_off = sum(1 for r in failed if not r.get("error") and r.get("stop_reason") == "max_tokens")
    unanswered = {
        "total": len(failed),
        "max_tokens": cut_off,
        "parse_failed": len(failed) - api_errors - cut_off,
        "api_error": api_errors,
    }
    per_case = {}
    for case in cases:
        case_rows = [r for r in rows if r["case"] == case["id"]]
        answers = [json.dumps(r.get("answer"), sort_keys=True) for r in case_rows]
        per_case[case["id"]] = {
            "engine": engine[case["id"]],
            "llm_answers": [r.get("answer") for r in case_rows],
            "spare_exact": sum(
                1 for r in case_rows if r.get("field_match", {}).get("spare") is True
            ),
            "consistent": len(set(answers)) == 1
            and not any(r.get("parse_failed") for r in case_rows),
            "spare_abs_errors": [r.get("spare_abs_error") for r in case_rows],
        }
    return {
        "calls": total,
        "scored_calls": scored,
        "parse_failures": total - len(parsed),
        "unanswered": unanswered,
        "field_exact_rate": field_rate,
        "all_fields_exact_rate": all_fields,
        "spare_exact_rate": field_rate["spare"],
        "spare_mean_abs_error": (sum(errors) / len(errors)) if errors else None,
        "consistent_cases": sum(1 for c in per_case.values() if c["consistent"]),
        "input_tokens": sum(r.get("input_tokens", 0) for r in rows),
        "output_tokens": sum(r.get("output_tokens", 0) for r in rows),
        "per_case": per_case,
    }


def print_table(summary: dict[str, Any], conditions: dict[str, Any]) -> None:
    print(
        f"model={conditions['model']} thinking={conditions['thinking']} "
        f"repeats={conditions['repeats']} cases={conditions['cases']} at={conditions['started_at']}"
    )
    print(
        f"{'case':<10} {'engine spare':>14} {'llm spare (per repeat)':<40} {'exact':>5} {'same':>5}"
    )
    for case_id, c in summary["per_case"].items():
        spares = [None if a is None else a["spare"] for a in c["llm_answers"]]
        print(
            f"{case_id:<10} {str(c['engine']['spare']):>14} {str(spares):<40} "
            f"{c['spare_exact']:>5} {'Y' if c['consistent'] else 'N':>5}"
        )
    rates = ", ".join(f"{k}={v:.0%}" for k, v in summary["field_exact_rate"].items())
    print(
        f"field exact: {rates}; all fields: {summary['all_fields_exact_rate']:.0%} "
        f"(over {summary['scored_calls']} scored calls, API errors excluded)"
    )
    print(
        f"spare mean abs error: {summary['spare_mean_abs_error']}; parse failures: "
        f"{summary['parse_failures']}/{summary['calls']}; consistent cases: "
        f"{summary['consistent_cases']}/{len(summary['per_case'])}; tokens in/out: "
        f"{summary['input_tokens']}/{summary['output_tokens']}"
    )
    u = summary["unanswered"]
    print(
        f"no answer: {u['total']}/{summary['calls']} (max_tokens={u['max_tokens']}, "
        f"parse_failed={u['parse_failed']}, api_error={u['api_error']})"
    )


if __name__ == "__main__":
    raise SystemExit(main())

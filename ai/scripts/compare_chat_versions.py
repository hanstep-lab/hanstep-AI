"""프롬프트 버전 비교표(#101): evaluate_chat.py 결과(JSONL) 여러 개를 읽어 두 설정을 나란히 놓는다.

API 호출 없음. 같은 variants 파일(예: specs/chat/variants_v44_v1.json)로 잰 결과들을 넘긴다.

    uv run python scripts/compare_chat_versions.py --base v4.2 --new v4.4 \
        --output artifacts/chat/v44_compare.md artifacts/chat/eval.jsonl artifacts/chat/remit.jsonl

- 회귀: base에서 모든 회차가 통과한 문항이 new에서 한 회차라도 실패하면 회귀로 적는다.
  항목은 도구 선택(toolMatch), 숫자 검사(numberCheck.unsupported가 비었는지), 송금 판정(remitCheck,
  정답·범위 밖 안내만 통과). 오류 회차(LLM_UPSTREAM_ERROR 등)는 두 쪽 모두 판정에서 뺀다.
- 숫자 검사는 근거가 측정 기록에 남는 도구만 본다. E-7-4 점수 도구(calculate_e74_score)는 LLM에 준
  결과가 agent.metrics에 없어 그 도구를 부른 회차는 숫자 검사를 '미측정'으로 두고, 대신 카드 값
  (E74_SCORE_CARD의 total·required)이 답변에 있는지 본다.
- 지연은 측정 스크립트가 잰 첫 delta·전체 시간(완료 회차)의 중앙값(파일별·분류별), 토큰은 턴 평균
  (송금 2턴은 턴마다).
- 실패 사례는 new 설정의 실패 회차와 회귀 문항의 base 회차 답변 원문을 적는다.
"""

from __future__ import annotations

import argparse
import io
import json
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

E74_TOOL = "calculate_e74_score"
USAGE_KEYS = ("inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens", "outputTokens")
REMIT_PASS = {"correct", "declined"}


def read_jsonl(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return lines[0], [r for r in lines[1:] if not r.get("warmup")]


def tool_names(row: dict[str, Any]) -> list[str]:
    follow = row.get("followUp") or {}
    return [t.get("toolName") for t in [*row.get("tools", []), *follow.get("tools", [])]]


def block_types(row: dict[str, Any]) -> list[str]:
    follow = row.get("followUp") or {}
    return [b.get("type") for b in [*(row.get("blocks") or []), *(follow.get("blocks") or [])]]


def number_ok(row: dict[str, Any]) -> bool | None:
    """숫자 검사 통과 여부. 근거가 남지 않는 E-7-4 도구를 부른 회차는 None(미측정)."""
    if E74_TOOL in tool_names(row):
        return None
    return not (row.get("numberCheck") or {}).get("unsupported")


def e74_card_in_answer(row: dict[str, Any]) -> bool | None:
    """E74_SCORE_CARD의 total·required가 답변에 있는지. 카드가 없으면 None."""
    card = next((b for b in row.get("blocks") or [] if b.get("type") == "E74_SCORE_CARD"), None)
    if card is None:
        return None
    answer = row.get("answer") or ""
    return all(str(card[k]) in answer for k in ("total", "required") if card.get(k) is not None)


def first_tool_ok(row: dict[str, Any]) -> bool:
    """처음 부른 도구가 기대 도구 중 하나인지(기대 도구가 없으면 아무것도 부르지 않았는지).

    도구가 실패해 다른 도구를 더 부르면 toolMatch(집합 일치)는 깨지지만, 모델이 처음 고른 도구는
    도구 결과와 관계없이 정해진다(예: 금융 정책 문서가 없는 로컬의 search_finance_policy).
    """
    expected = set(row.get("expectedTools") or [])
    called = [t.get("toolName") for t in row.get("tools", [])]
    return called[0] in expected if called else not expected


def checks(row: dict[str, Any]) -> dict[str, bool | None]:
    out: dict[str, bool | None] = {
        "tool": bool(row.get("toolMatch")),
        "first": first_tool_ok(row),
        "number": number_ok(row),
    }
    if row.get("remitCheck"):
        out["remit"] = row["remitCheck"]["outcome"] in REMIT_PASS
    return out


def done_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r.get("status") == "done"]


def ratio(values: list[bool | None]) -> str:
    known = [v for v in values if v is not None]
    if not known:
        return "미측정"
    return f"{sum(known)}/{len(known)}"


def median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}"


def _pct(new: float | None, base: float | None) -> str:
    if new is None or not base:
        return "-"
    return f"{(new - base) / base * 100:+.1f}%"


def turn_usages(row: dict[str, Any]) -> list[dict[str, Any]]:
    turns = [row.get("usage") or {}]
    follow = row.get("followUp")
    if follow and follow.get("status") == "done":
        turns.append(follow.get("usage") or {})
    return turns


def fx_numbers(rows: list[dict[str, Any]]) -> dict[str, Any]:
    fx = [r for r in rows if r.get("category") == "fx"]
    direction = [((r.get("fxCheck") or {}).get("direction") or {}) for r in fx]
    return {
        "n": len(fx),
        "summaryTool": sum(1 for r in fx if "fx_rate_summary" in tool_names(r)),
        "getFxRate": sum(1 for r in fx if "get_fx_rate" in tool_names(r)),
        "card": sum(1 for r in fx if "FX_SUMMARY_CARD" in block_types(r)),
        "numberOk": sum(1 for r in fx if number_ok(r)),
        "mismatches": sum(d.get("mismatches", 0) for d in direction),
        "matches": sum(d.get("matches", 0) for d in direction),
        "noDirection": sum(d.get("noDirection", 0) for d in direction),
        "advice": sum(len((r.get("fxCheck") or {}).get("advice") or []) for r in fx),
    }


def forbidden(
    rows: list[dict[str, Any]],
) -> tuple[list[tuple[dict[str, Any], dict[str, str]]], int]:
    """(행, 금지 표현) 목록과 부정문이라 세지 않은 수(송금 2턴은 두 답을 합쳐 본 값)."""
    hits = [(r, h) for r in rows for h in (r.get("forbidden") or {}).get("hits") or []]
    negated = sum(len((r.get("forbidden") or {}).get("negated") or []) for r in rows)
    return hits, negated


def failed(row: dict[str, Any]) -> list[str]:
    """이 회차가 걸린 항목."""
    reasons = [k for k, v in checks(row).items() if v is False]
    if (row.get("forbidden") or {}).get("hits"):
        reasons.append("forbidden")
    if ((row.get("fxCheck") or {}).get("direction") or {}).get("mismatches"):
        reasons.append("direction")
    if e74_card_in_answer(row) is False:
        reasons.append("e74CardValue")
    return reasons


def _answer_block(row: dict[str, Any], reasons: list[str]) -> list[str]:
    follow = row.get("followUp") or {}
    calls = ", ".join(
        f"{t.get('toolName')}({t.get('status')})"
        for t in [*row.get("tools", []), *follow.get("tools", [])]
    )
    out = [
        f"**{row['_set']} {row['questionId']}#{row['repeat']} {row['variant']}** — "
        f"{', '.join(reasons)}",
        "",
        f"- 도구: {calls or '없음'} / 기대: {', '.join(row.get('expectedTools') or []) or '없음'}",
        f"- 카드: {', '.join(block_types(row)) or '없음'}",
    ]
    unsupported = (row.get("numberCheck") or {}).get("unsupported") or []
    if unsupported and number_ok(row) is False:
        out.append(f"- 근거 없는 숫자: {', '.join(unsupported)}")
    if row.get("remitCheck"):
        out.append(f"- 송금 판정: {row['remitCheck']['outcome']} {row['remitCheck'].get('reason')}")
    for h in (row.get("forbidden") or {}).get("hits") or []:
        out.append(f"- 금지 표현({h['kind']}): {h['text']}")
    for m in ((row.get("fxCheck") or {}).get("direction") or {}).get("mentions") or []:
        if m["result"] in ("mismatch", "insufficientData"):
            out.append(f"- 방향 불일치: {m['currency']} {m['period']} {m['said']}≠{m['expected']}")
    out += ["", "> " + (row.get("answer") or "(빈 답변)").replace("\n", "\n> ")]
    if follow:
        out += ["", f"2턴 사용자: {follow.get('message')}", ""]
        out += ["> " + (follow.get("answer") or "(빈 답변)").replace("\n", "\n> ")]
    return [*out, ""]


def render(
    files: list[tuple[Path, dict[str, Any], list[dict[str, Any]]]], base: str, new: str
) -> str:
    out = [f"# 프롬프트 {base} vs {new} 비교", "", "## 측정 파일", ""]
    out += ["| 파일 | 질문 세트 | 시작(UTC) | 상태 | 문항 × 반복 | 행 | 오류 행 | 비용(USD) |"]
    out += ["|---|---|---|---|---|---|---|---|"]
    for path, cond, rows in files:
        qs = cond.get("questionSet") or {}
        errors = Counter(r.get("errorCode") for r in rows if r.get("status") != "done")
        out.append(
            f"| {path.name} | {qs.get('file')} ({qs.get('version')}) | {cond.get('startedAt')} | "
            f"{cond.get('status')} | {len(cond.get('questions') or [])} × {cond.get('repeats')} | "
            f"{len(rows)} | {sum(errors.values())} {dict(errors) or ''} | "
            f"{cond.get('spentUsd', 0):.4f} |"
        )
    variants = {v["name"]: v for _, cond, _ in files for v in cond.get("variants") or []}
    for name in (base, new):
        v = variants.get(name, {})
        out.append(
            f"- {name}: model={v.get('model')}, thinking={v.get('thinking')}, "
            f"maxTokens={v.get('maxTokens')}, judgmentFacts={v.get('judgmentFacts')}, "
            f"promptVersion={v.get('promptVersion')}"
        )
    # 문항 id는 세트마다 겹칠 수 있다(eval O01과 policy O01). 세트(파일 이름)와 함께 묶는다.
    for path, _, rows in files:
        for r in rows:
            r["_set"] = path.stem
    everything = [r for _, _, rows in files for r in rows]
    by = {name: [r for r in everything if r["variant"] == name] for name in (base, new)}

    # --- 회귀 ---
    out += ["", "## 회귀(base 전 회차 통과 → new 실패)", ""]
    regressions: list[tuple[str, str, str, str]] = []
    keys = dict.fromkeys((r["_set"], r["questionId"], r["category"]) for r in everything)

    def of(name: str, set_name: str, qid: str) -> list[dict[str, Any]]:
        return done_rows([r for r in by[name] if r["_set"] == set_name and r["questionId"] == qid])

    for set_name, qid, _category in keys:
        b_rows, n_rows = of(base, set_name, qid), of(new, set_name, qid)
        for item in ("tool", "first", "number", "remit"):
            b_vals = [checks(r).get(item) for r in b_rows]
            n_vals = [checks(r).get(item) for r in n_rows]
            b_known = [v for v in b_vals if v is not None]
            n_known = [v for v in n_vals if v is not None]
            if b_known and all(b_known) and n_known and not all(n_known):
                regressions.append((set_name, qid, item, f"{ratio(b_vals)} → {ratio(n_vals)}"))
    if regressions:
        out += ["| 세트 | 문항 | 항목 | base → new |", "|---|---|---|---|"]
        out += [f"| {st} | {q} | {item} | {change} |" for st, q, item, change in regressions]
    else:
        out.append("- 없음")

    # --- 문항별 ---
    out += ["", "## 문항별", ""]
    out += [
        f"| 세트 | 문항 | 분류 | {base} 도구 | {new} 도구 | {base} 첫 도구 | {new} 첫 도구 | "
        f"{base} 숫자 | {new} 숫자 | {base} 송금 | {new} 송금 | {base} 카드 | {new} 카드 | "
        f"{new} 호출한 도구 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for set_name, qid, category in keys:
        cells = []
        rows_of = {name: of(name, set_name, qid) for name in (base, new)}
        for item in ("tool", "first", "number", "remit"):
            for name in (base, new):
                values = [checks(r).get(item) for r in rows_of[name]]
                cells.append(ratio(values) if any(v is not None for v in values) else "-")
        for name in (base, new):
            types = Counter(t for r in rows_of[name] for t in set(block_types(r)))
            cells.append(", ".join(f"{t} {c}" for t, c in sorted(types.items())) or "-")
        called = Counter(t for r in rows_of[new] for t in tool_names(r))
        cells.append(", ".join(f"{t} {c}" for t, c in sorted(called.items())) or "없음")
        out.append(f"| {set_name} | {qid} | {category} | " + " | ".join(cells) + " |")

    # --- 항목별 요약 ---
    out += ["", "## 항목별 요약", "", f"| 항목 | {base} | {new} |", "|---|---|---|"]
    stats = {name: done_rows(rows) for name, rows in by.items()}
    for item, label in (
        ("tool", "도구 선택 일치"),
        ("first", "첫 도구 일치"),
        ("number", "숫자 검사 통과"),
        ("remit", "송금 판정 통과"),
    ):
        out.append(
            f"| {label} | "
            + " | ".join(ratio([checks(r).get(item) for r in stats[name]]) for name in (base, new))
            + " |"
        )
    outcomes = {
        name: Counter(r["remitCheck"]["outcome"] for r in stats[name] if r.get("remitCheck"))
        for name in (base, new)
    }
    if any(outcomes.values()):
        out.append(
            "| 송금 판정 분포 | "
            + " | ".join(
                ", ".join(f"{k} {v}" for k, v in sorted(outcomes[n].items())) or "-"
                for n in (base, new)
            )
            + " |"
        )
        out.append(
            "| 송금 판정 사람 확인(needsReview) | "
            + " | ".join(
                str(sum(1 for r in stats[n] if (r.get("remitCheck") or {}).get("needsReview")))
                for n in (base, new)
            )
            + " |"
        )
    fx = {name: fx_numbers(stats[name]) for name in (base, new)}
    for key, label in (
        ("summaryTool", "환율 문항 fx_rate_summary 호출"),
        ("getFxRate", "환율 문항 get_fx_rate 호출"),
        ("card", "환율 문항 FX_SUMMARY_CARD"),
        ("numberOk", "환율 문항 숫자 검사 통과"),
    ):
        out.append(
            f"| {label} | " + " | ".join(f"{fx[n][key]}/{fx[n]['n']}" for n in (base, new)) + " |"
        )
    out.append(
        "| 환율 방향 불일치(일치·방향 낱말 없음) | "
        + " | ".join(
            f"{fx[n]['mismatches']} ({fx[n]['matches']}·{fx[n]['noDirection']})"
            for n in (base, new)
        )
        + " |"
    )
    out.append(
        "| 예측·권유 자동 표시(환율 문항) | "
        + " | ".join(str(fx[n]["advice"]) for n in (base, new))
        + " |"
    )
    hits = {name: forbidden(stats[name]) for name in (base, new)}
    for kind in ("realtime", "forecast", "preferential"):
        out.append(
            f"| 금지 표현 {kind}(부정문 제외) | "
            + " | ".join(
                str(sum(1 for _, h in hits[n][0] if h["kind"] == kind)) for n in (base, new)
            )
            + " |"
        )
    out.append(
        "| 금지 표현 부정문(세지 않음, 사람 확인) | "
        + " | ".join(str(hits[n][1]) for n in (base, new))
        + " |"
    )
    for card in ("REMITTANCE_CONFIRM", "E74_SCORE_CARD"):
        out.append(
            f"| {card}가 나온 회차 | "
            + " | ".join(
                str(sum(1 for r in stats[n] if card in block_types(r))) for n in (base, new)
            )
            + " |"
        )
    out.append(
        "| E-7-4 카드 값(total·required)이 답변에 있음 | "
        + " | ".join(ratio([e74_card_in_answer(r) for r in stats[n]]) for n in (base, new))
        + " |"
    )

    # --- 지연 ---
    out += ["", "## 지연(완료 회차, 측정 스크립트 기준 ms 중앙값)", ""]
    out += [
        f"| 세트 | {base} 첫 delta | {new} 첫 delta | 차이 | "
        f"{base} 전체 | {new} 전체 | 차이 | 회차 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    for path, _, rows in files:
        groups.append((path.stem, rows))
        categories = list(dict.fromkeys(r["category"] for r in rows))
        if len(categories) > 1:
            groups += [
                (f"{path.stem} · {c}", [r for r in rows if r["category"] == c]) for c in categories
            ]
    groups.append(("전체", everything))
    for label, rows in groups:
        med: dict[str, tuple[float | None, float | None, int]] = {}
        for name in (base, new):
            mine = done_rows([r for r in rows if r["variant"] == name])
            first = [r["timings"]["firstDeltaMs"] for r in mine if "firstDeltaMs" in r["timings"]]
            total = [r["timings"]["doneMs"] for r in mine if "doneMs" in r["timings"]]
            med[name] = (median(first), median(total), len(mine))
        (b_first, b_total, b_n), (n_first, n_total, n_n) = med[base], med[new]
        out.append(
            f"| {label} | {_ms(b_first)} | {_ms(n_first)} | {_pct(n_first, b_first)} | "
            f"{_ms(b_total)} | {_ms(n_total)} | {_pct(n_total, b_total)} | {b_n} / {n_n} |"
        )

    # --- 토큰 ---
    out += ["", "## 토큰(턴 평균, 완료 회차)", ""]
    out += [
        "| 설정 | 턴 | 입력(캐시 아님) | 캐시 읽기 | 캐시 쓰기 | 입력 합계 | 출력 "
        "| 비용(USD/회차) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name in (base, new):
        turns = [u for r in stats[name] for u in turn_usages(r)]
        n = len(turns) or 1
        avg = {k: sum(u.get(k, 0) or 0 for u in turns) / n for k in USAGE_KEYS}
        total_in = (
            avg["inputTokens"] + avg["cacheReadInputTokens"] + avg["cacheCreationInputTokens"]
        )
        cost = sum(r.get("costUsd", 0) for r in stats[name]) / (len(stats[name]) or 1)
        out.append(
            f"| {name} | {len(turns)} | {avg['inputTokens']:.0f} | "
            f"{avg['cacheReadInputTokens']:.0f} | {avg['cacheCreationInputTokens']:.0f} | "
            f"{total_in:.0f} | {avg['outputTokens']:.0f} | {cost:.5f} |"
        )

    # --- 금지 표현 문장 ---
    out += ["", "## 금지 표현 문장(부정문 제외)", ""]
    any_hit = False
    for name in (base, new):
        for row, hit in hits[name][0]:
            any_hit = True
            out.append(f"- {name} {row['questionId']}#{row['repeat']} {hit['kind']}: {hit['text']}")
    if not any_hit:
        out.append("- 없음")
    out += ["", "부정문으로 빠진 문장(사람 확인용):", ""]
    for name in (base, new):
        for row in stats[name]:
            for item in (row.get("forbidden") or {}).get("negated") or []:
                out.append(
                    f"- {name} {row['questionId']}#{row['repeat']} {item['kind']}: {item['text']}"
                )

    # --- 실패 사례 ---
    out += ["", f"## 실패 사례 원문({new} 실패 회차 + 회귀 문항의 {base} 회차)", ""]
    regressed = {(st, q) for st, q, _, _ in regressions}
    for row in everything:
        reasons = failed(row) if row.get("status") == "done" else [f"오류 {row.get('errorCode')}"]
        if row["variant"] == new and reasons:
            out += _answer_block(row, reasons)
        elif row["variant"] == base and (row["_set"], row["questionId"]) in regressed:
            out += _answer_block(row, reasons or ["(통과, 비교용)"])
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--base", required=True, help="variant name, e.g. v4.2")
    parser.add_argument("--new", required=True, help="variant name, e.g. v4.4")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("inputs", type=Path, nargs="+")
    args = parser.parse_args(argv)
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    files = [(path, *read_jsonl(path)) for path in args.inputs]
    args.output.write_text(render(files, args.base, args.new), encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

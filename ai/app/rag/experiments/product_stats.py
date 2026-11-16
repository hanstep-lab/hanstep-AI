"""금융상품 검색 실험 보고서의 비교 통계: 단어 겹침, 정확 McNemar, 부트스트랩 신뢰구간, 한·영 짝.

모두 이미 만든 보고서(JSON)의 문항별 결과로만 계산한다. 검색·임베딩은 다시 하지 않는다.

- 단어 겹침: 질문 ↔ goldQuote의 글자 2-gram Jaccard(공백 제거·소문자). 한국어 문항에만 계산하고
  그 중앙값 기준으로 낮음(< 중앙값)/높음(≥ 중앙값)으로 나눈다. 영어 문항은 '해당 없음'.
- 정확 McNemar: 이진 지표에서 b = A만 맞힘, c = B만 맞힘, p = 양측 이항검정(n = b + c, 0.5).
  MRR@5는 이진값이 아니라 McNemar를 계산하지 않는다.
- 부트스트랩: 문항을 복원 추출해 방식별 평균의 95% 백분위 구간을 낸다(같은 추출을 모든 방식에 씀).
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from app.rag.experiments.product_corpus import Question, match_key

OVERLAP_NOT_APPLICABLE = "해당 없음"
BOOTSTRAP_ROUNDS = 10_000
BOOTSTRAP_SEED = 20261116
# 미리 정한 확인 지표. 앞의 셋은 이진값이라 McNemar를 계산한다.
BINARY_METRICS = ("recall@k", "evidence@k", "recall@1")
METRICS = ("recall@k", "evidence@k", "recall@1", "rr")
METRIC_LABELS = {
    "recall@k": "Recall@5",
    "evidence@k": "근거 적중@5",
    "recall@1": "Recall@1",
    "rr": "MRR@5",
}
SIGNIFICANCE = 0.05
# 판정 원칙(미리 정함): v2-ko에서 ③의 Recall@5 적중 문항이 ①보다 이만큼 이상 적을 때만 재검토.
REVIEW_MARGIN = 2


def char_bigrams(text: str) -> set[str]:
    key = match_key(text).lower()
    return {key[i : i + 2] for i in range(len(key) - 1)}


def bigram_jaccard(a: str, b: str) -> float:
    x, y = char_bigrams(a), char_bigrams(b)
    union = x | y
    return len(x & y) / len(union) if union else 0.0


def overlap_groups(questions: Sequence[Question]) -> dict[str, dict[str, Any]]:
    """문항별 겹침 값과 낮음/높음/해당 없음. 기준 중앙값은 한국어 문항의 중앙값이다."""
    ko = {q.id: bigram_jaccard(q.question, q.gold_quote) for q in questions if q.language == "ko"}
    median = statistics.median(ko.values()) if ko else None
    result: dict[str, dict[str, Any]] = {}
    for question in questions:
        if question.id in ko and median is not None:
            value = ko[question.id]
            group = "낮음" if value < median else "높음"
            result[question.id] = {"jaccard": round(value, 4), "group": group, "median": median}
        else:
            result[question.id] = {"jaccard": None, "group": OVERLAP_NOT_APPLICABLE}
    return result


def exact_mcnemar(b: int, c: int) -> float:
    """정확 McNemar 양측 p값. 불일치 쌍이 없으면 1.0."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / float(2**n)
    return min(1.0, 2.0 * tail)


def compare(
    rows_a: Mapping[str, Mapping[str, Any]],
    rows_b: Mapping[str, Mapping[str, Any]],
    ids: Sequence[str],
    metric: str,
) -> dict[str, Any]:
    """같은 문항에서 이진 ``metric``을 비교한다. b = A만 맞힘, c = B만 맞힘."""
    b = sum(1 for q in ids if rows_a[q][metric] and not rows_b[q][metric])
    c = sum(1 for q in ids if rows_b[q][metric] and not rows_a[q][metric])
    p = exact_mcnemar(b, c)
    return {"b": b, "c": c, "p": p, "significant": p < SIGNIFICANCE}


def bootstrap_ci(
    values: Mapping[str, Sequence[float]],
    rounds: int = BOOTSTRAP_ROUNDS,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, dict[str, float]]:
    """방식별 문항 값(같은 문항 순서)의 평균과 부트스트랩 95% 백분위 구간."""
    keys = list(values)
    matrix = np.asarray([values[key] for key in keys], dtype=np.float64)
    n = matrix.shape[1]
    if n == 0:
        return {
            key: {"mean": float("nan"), "low": float("nan"), "high": float("nan")} for key in keys
        }
    rng = np.random.default_rng(seed)
    samples = rng.integers(0, n, size=(rounds, n))
    means = matrix[:, samples].mean(axis=2)
    return {
        key: {
            "mean": float(matrix[i].mean()),
            "low": float(np.percentile(means[i], 2.5)),
            "high": float(np.percentile(means[i], 97.5)),
        }
        for i, key in enumerate(keys)
    }


def set_statistics(
    rows_by_variant: Mapping[str, Mapping[str, Mapping[str, Any]]],
    ids: Sequence[str],
    pairs: Sequence[tuple[str, str]] = (("1", "3"), ("2", "3")),
    rounds: int = BOOTSTRAP_ROUNDS,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """문항 집합 하나의 방식 비교(McNemar)와 방식별 부트스트랩 구간."""
    variants = [key for key in rows_by_variant if all(q in rows_by_variant[key] for q in ids)]
    comparisons = {
        f"{a}vs{b}": {
            metric: compare(rows_by_variant[a], rows_by_variant[b], ids, metric)
            for metric in BINARY_METRICS
        }
        for a, b in pairs
        if a in variants and b in variants
    }
    intervals = {
        metric: bootstrap_ci(
            {key: [float(rows_by_variant[key][q][metric]) for q in ids] for key in variants},
            rounds,
            seed,
        )
        for metric in METRICS
    }
    hits = {
        key: {
            metric: int(sum(rows_by_variant[key][q][metric] for q in ids))
            for metric in BINARY_METRICS
        }
        for key in variants
    }
    return {"n": len(ids), "hits": hits, "comparisons": comparisons, "intervals": intervals}


def pair_comparison(
    rows_by_variant: Mapping[str, Mapping[str, Mapping[str, Any]]],
    pairs: Sequence[tuple[str, str]],
) -> dict[str, Any]:
    """한·영 짝(같은 pairId)마다 방식별 정답 문서 순위를 비교한다. 순위 없음은 6으로 본다."""

    def rank(row: Mapping[str, Any]) -> int:
        return int(row["doc_rank"]) if row["doc_rank"] is not None else 6

    def in5(row: Mapping[str, Any]) -> bool:
        return bool(row["recall@k"])

    summary: dict[str, Any] = {}
    for key, rows in rows_by_variant.items():
        valid = [(ko, en) for ko, en in pairs if ko in rows and en in rows]
        summary[key] = {
            "pairs": len(valid),
            "same": sum(1 for ko, en in valid if rank(rows[ko]) == rank(rows[en])),
            "ko_better": sum(1 for ko, en in valid if rank(rows[ko]) < rank(rows[en])),
            "en_better": sum(1 for ko, en in valid if rank(rows[ko]) > rank(rows[en])),
            "both_top1": sum(
                1 for ko, en in valid if rows[ko]["recall@1"] and rows[en]["recall@1"]
            ),
            # 정답 문서가 상위 5 안에 있는지(Recall@5)로 나눈 짝 수.
            "top5": {
                "ko_only": sum(1 for ko, en in valid if in5(rows[ko]) and not in5(rows[en])),
                "en_only": sum(1 for ko, en in valid if in5(rows[en]) and not in5(rows[ko])),
                "both": sum(1 for ko, en in valid if in5(rows[ko]) and in5(rows[en])),
                "neither": sum(1 for ko, en in valid if not in5(rows[ko]) and not in5(rows[en])),
            },
            "detail": [
                {
                    "ko": ko,
                    "en": en,
                    "ko_rank": rows[ko]["doc_rank"],
                    "en_rank": rows[en]["doc_rank"],
                }
                for ko, en in valid
            ],
        }
    return summary


def review_decision(ko_stats: Mapping[str, Any]) -> dict[str, Any]:
    """판정 원칙: 선택은 ③. v2-ko에서 ③ Recall@5 적중이 ①보다 REVIEW_MARGIN 이상 적으면 재검토."""
    hits = ko_stats["hits"]
    baseline, chosen = hits["1"]["recall@k"], hits["3"]["recall@k"]
    review = baseline - chosen >= REVIEW_MARGIN
    return {
        "rule": f"v2-ko에서 ③ Recall@5 적중이 ①보다 {REVIEW_MARGIN}문항 이상 적으면 재검토",
        "recall5_hits": {"1": baseline, "3": chosen},
        "review": review,
        "decision": "재검토" if review else "선택 ③ 유지",
    }


PRE_MEASUREMENT_CHANGES = (
    "v2 형식 로더: 최상위 배열, hasProductName → namesProduct, page 없음('-'), "
    "language·pairId 유지",
    "goldDocument 매핑에서 끝의 '.pdf'를 뗌(v1 문항에는 영향 없음)",
    "언어별·상품명 유무별·한국어 단어 겹침(낮음/높음) 집계, 한·영 짝 비교",
    "단어 겹침은 한국어 문항에만 계산(영어는 질문·근거 언어가 달라 '해당 없음')",
    "정확 McNemar(①vs③, ②vs③)와 방식별 부트스트랩 95% 구간",
    "한·영 짝을 정답 문서 상위 5 포함 여부로 나눈 집계(ko만/en만/둘 다/둘 다 아님)",
    "평가에서 문항을 빼는 옵션(--exclude-ids, 이유를 보고서에 기록)",
)

RowsByVariant = dict[str, dict[str, Mapping[str, Any]]]


def rows_by_variant(report: Mapping[str, Any]) -> RowsByVariant:
    return {
        key: {row["id"]: row for row in result["rows"]}
        for key, result in report["variants"].items()
    }


def question_sets(
    name: str, report: Mapping[str, Any]
) -> list[tuple[str, RowsByVariant, list[str]]]:
    """보고서 하나를 (집합 이름, 방식별 행, 문항 id)로 나눈다. 언어가 있으면 언어별도 만든다."""
    rows = rows_by_variant(report)
    first = next(iter(report["variants"].values()))["rows"]
    languages = sorted({row["language"] for row in first if row.get("language")})
    sets = [
        (f"{name}-{lang}", rows, [r["id"] for r in first if r.get("language") == lang])
        for lang in languages
    ]
    sets.append((name, rows, [row["id"] for row in first]))
    return sets


def ko_en_pairs(report: Mapping[str, Any]) -> list[tuple[str, str]]:
    """한국어 문항과 pairId가 가리키는 영어 문항의 (ko id, en id)."""
    first = next(iter(report["variants"].values()))["rows"]
    ids = {row["id"] for row in first}
    return [
        (row["id"], row["pair_id"])
        for row in first
        if row.get("language") == "ko" and row.get("pair_id") in ids
    ]


def build_statistics(
    reports: Mapping[str, Mapping[str, Any]],
    *,
    decision_set: str = "v2-ko",
    rounds: int = BOOTSTRAP_ROUNDS,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """보고서들(이름 → JSON)의 집합별 통계, 한·영 짝 비교, 판정."""
    sets: dict[str, Any] = {}
    pairs: dict[str, Any] = {}
    for name, report in reports.items():
        for set_name, rows, ids in question_sets(name, report):
            sets[set_name] = set_statistics(rows, ids, rounds=rounds, seed=seed)
        found = ko_en_pairs(report)
        if found:
            pairs[name] = pair_comparison(rows_by_variant(report), found)
    decision = review_decision(sets[decision_set]) if decision_set in sets else None
    return {
        "bootstrap": {"rounds": rounds, "seed": seed, "interval": "95% percentile"},
        "significance": SIGNIFICANCE,
        "sources": {name: report.get("questions_file") for name, report in reports.items()},
        "pre_measurement_changes": list(PRE_MEASUREMENT_CHANGES),
        "excluded": {name: list(report.get("excluded") or []) for name, report in reports.items()},
        "sets": sets,
        "pairs": pairs,
        "decision": decision,
    }


def render_statistics(stats: Mapping[str, Any]) -> str:
    boot = stats["bootstrap"]
    lines = [
        "# 금융상품 RAG 검색 비교 통계",
        "",
        f"- 부트스트랩: {boot['rounds']}회, 시드 {boot['seed']}, {boot['interval']} 구간",
        f"- 정확 McNemar 양측, 유의 수준 {stats['significance']}. "
        "b = 앞 방식만 맞힘, c = 뒤 방식(③)만 맞힘",
        "- MRR@5는 이진값이 아니라 McNemar 없이 구간만 낸다",
        "",
        "## 측정 전 코드 수정(공개)",
        "",
        *[f"- {change}" for change in stats["pre_measurement_changes"]],
        "",
        "## 원본",
        "",
        *[f"- {name}: `{source}`" for name, source in stats["sources"].items()],
        "",
        "## 제외 문항(측정 전 결정)",
        "",
    ]
    excluded = [(name, item) for name, items in stats["excluded"].items() for item in items]
    lines += [f"- {name} {item['id']}: {item['reason']}" for name, item in excluded] or ["- 없음"]
    for set_name, data in stats["sets"].items():
        note = " — 실사용 언어 조건, 선택에 쓰지 않음" if set_name.endswith("-en") else ""
        keys = list(data["hits"])
        lines += [
            "",
            f"## {set_name} (n={data['n']}){note}",
            "",
            "| 지표 | " + " | ".join(keys) + " |",
            "|---|" + "---|" * len(keys),
        ]
        for metric in METRICS:
            cells = []
            for key in keys:
                ci = data["intervals"][metric][key]
                hit = (
                    f"{data['hits'][key][metric]}/{data['n']} " if metric in BINARY_METRICS else ""
                )
                cells.append(f"{hit}{ci['mean']:.3f} [{ci['low']:.3f}, {ci['high']:.3f}]")
            lines.append(f"| {METRIC_LABELS[metric]} | " + " | ".join(cells) + " |")
        if data["comparisons"]:
            lines += ["", "| 비교 | 지표 | b | c | p | 판정 |", "|---|---|---|---|---|---|"]
            for pair, metrics in data["comparisons"].items():
                for metric, result in metrics.items():
                    verdict = "유의함" if result["significant"] else "유의하지 않음"
                    lines.append(
                        f"| {pair} | {METRIC_LABELS[metric]} | {result['b']} | {result['c']} | "
                        f"{result['p']:.4f} | {verdict} |"
                    )
    for name, summary in stats["pairs"].items():
        lines += [
            "",
            f"## 한·영 짝 비교 ({name}, 정답 문서 순위, 상위 5 밖은 6으로 비교)",
            "",
            "| 방식 | 짝 | 상위5 ko만 | en만 | 둘 다 | 둘 다 아님 | 순위 같음 | ko가 높음 "
            "| en이 높음 | 둘 다 1위 |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for key, s in summary.items():
            top5 = s["top5"]
            lines.append(
                f"| {key} | {s['pairs']} | {top5['ko_only']} | {top5['en_only']} | "
                f"{top5['both']} | {top5['neither']} | {s['same']} | {s['ko_better']} | "
                f"{s['en_better']} | {s['both_top1']} |"
            )
    decision = stats["decision"]
    if decision:
        hits = decision["recall5_hits"]
        lines += [
            "",
            "## 판정(미리 정한 원칙)",
            "",
            f"- 원칙: {decision['rule']}",
            f"- Recall@5 적중: ① {hits['1']}, ③ {hits['3']} → **{decision['decision']}**",
        ]
    return "\n".join(lines).rstrip() + "\n"

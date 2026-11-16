"""금융상품 추천 오프라인 평가(이슈 #56): 조건표로 상위 5 추천의 조건 위반을 코드로 판정한다.

조건표(specs/rag/product_catalog_v1.json)의 값으로 추천 상품마다 조건을 판정한다.
판정은 ok / violation / unknown(조건표 값이 null이라 판정 불가) 셋이다.

- 추천 대상: recommendable=false 상품이나 조건표에 없는 문서(약관)를 추천하면 위반.
- 상품 종류(kind): 요청이 적금·예금을 정하면 다른 종류는 위반.
- 외국인: foreignerEligible이 false일 때만 위반(explicit·inferred는 통과, null은 판정 불가).
- 비자: visaTypes에 프로필 비자가 없으면 위반(null은 판정 불가).
- 나이: ageMin·ageMax 중 있는 값으로 판정(둘 다 null이면 판정 불가).
- 체류 기간: 가장 짧은 계약기간(termOptions의 최소, 없으면 termMonthsMin)이
  남은 체류 개월보다 길면 위반.
- 여유자금(월): 적금의 monthlyMinKrw가 프로필 여유자금보다 크면 위반. 예금은 월 흐름과 목돈의
  단위가 달라 비교하지 않는다(프로필에 목돈 값이 없어 최소 가입액 조건을 적용하지 않음).
- 요청 기간(3개월 이하 등): 가장 짧은 계약기간이 요청 상한보다 길면 위반.
- 요청 월 납입액: 월 납입 범위 밖이면 위반(noLimit 상한은 통과). 요청액이 여유자금보다 크면
  어떤 상품이든 위반(그 프로필에는 추천할 상품이 없다).
- 적립 방식: savingType이 요청과 다르면 위반.

gold 후보 = 위반이 하나도 없고, 요청 조건(요청 기간·월 납입액·적립 방식)에 판정 불가가 없고,
요청이 정한 goldRequires(예: foreignerEligible=explicit)를 만족하는 상품.
프로필 조건(나이·비자)의 판정 불가는 gold에서 빼지 않는다(문서가 그 제한을 두지 않음).
gold는 사람이 확인해 질문 세트에 고정한다. 평가는 질문 세트의 gold만 쓴다.

지표: 상위 5의 위반 상품 수·위반 수·판정 불가 수(조건별), gold Recall@5
(= |상위 5 ∩ gold| / min(|gold|, 5), gold가 있는 문항만),
gold 0개 문항에서 아무것도 추천하지 않은 비율.

    uv run python scripts/product_recommend_eval.py gold          # gold 후보 표(모델 없음)
    uv run python scripts/product_recommend_eval.py gold --questions <질문 세트> --write <새 파일>
        # gold를 후보로 채운 사본(goldConfirmed=false). 파일 내용은 출력하지 않는다.
    uv run python scripts/product_recommend_eval.py baseline \\
        --corpus ../../rag-data/products --revision 5617a9f61b028005a4858fdac845db406aefb181

baseline(①): #47의 ③ 검색(dense + BM25 RRF)으로 청크를 찾고, 약관 청크를 뺀 뒤 상품별 최고 점수로
순위를 매긴 상위 5개를 필터 없이 추천한다. 로컬 임베딩만 쓰고 LLM은 부르지 않는다.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import _bootstrap

AI_ROOT = _bootstrap.AI_ROOT
CATALOG_PATH = AI_ROOT / "specs" / "rag" / "product_catalog_v1.json"
QUESTIONS_PATH = AI_ROOT / "specs" / "rag" / "product_recommend_questions_v1.json"
DEFAULT_OUTPUT_DIR = AI_ROOT / "artifacts" / "rag"
DEFAULT_CACHE_DIR = DEFAULT_OUTPUT_DIR / "embedding-cache"
TOP_K = 5

OK, VIOLATION, UNKNOWN = "ok", "violation", "unknown"
# 요청 조건: 판정 불가면 gold 후보에서 뺀다.
REQUEST_CONDITIONS = ("requestTerm", "requestMonthly", "savingType")
CONDITIONS = (
    "recommendable",
    "kind",
    "foreigner",
    "visa",
    "age",
    "stayTerm",
    "spare",
    *REQUEST_CONDITIONS,
)

Product = Mapping[str, Any]
Profile = Mapping[str, Any]
Request = Mapping[str, Any]
Recommendation = Mapping[str, Any]


def value(product: Product, field: str) -> Any:
    entry = product.get(field)
    return entry.get("value") if isinstance(entry, Mapping) else None


def no_limit(product: Product, field: str) -> bool:
    entry = product.get(field)
    return bool(isinstance(entry, Mapping) and entry.get("noLimit"))


def months_between(start: date, end: date) -> int:
    """start부터 end까지 채운 개월 수(끝날 일자가 시작 일자보다 앞이면 한 달 덜 셈)."""
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        months -= 1
    return max(months, 0)


def remaining_stay_months(profile: Profile, as_of: date) -> int:
    return months_between(as_of, date.fromisoformat(profile["stayExpiry"]))


def shortest_term(product: Product) -> int | None:
    options = value(product, "termOptions")
    if options:
        return int(min(options))
    term = value(product, "termMonthsMin")
    return None if term is None else int(term)


def _upper(limit: Any, unlimited: bool, amount: int) -> str:
    if unlimited:
        return OK
    if limit is None:
        return UNKNOWN
    return OK if amount <= limit else VIOLATION


def check_product(
    product: Product | None, profile: Profile, request: Request, as_of: date
) -> dict[str, str]:
    """상품 하나를 프로필·요청 조건으로 판정한다. product가 None이면 조건표에 없는 문서(약관)."""
    if product is None:
        return {"recommendable": VIOLATION}
    conditions = request.get("conditions", {})
    result: dict[str, str] = {}
    result["recommendable"] = OK if product.get("recommendable", True) else VIOLATION

    kind = value(product, "kind")
    wanted_kind = conditions.get("kind")
    if wanted_kind:
        result["kind"] = OK if kind == wanted_kind else VIOLATION

    foreigner = value(product, "foreignerEligible")
    result["foreigner"] = (
        UNKNOWN if foreigner is None else (VIOLATION if foreigner is False else OK)
    )

    visas = value(product, "visaTypes")
    result["visa"] = UNKNOWN if visas is None else (OK if profile["visa"] in visas else VIOLATION)

    age_min, age_max = value(product, "ageMin"), value(product, "ageMax")
    if age_min is None and age_max is None:
        result["age"] = UNKNOWN
    else:
        age = profile["age"]
        too_young = age_min is not None and age < age_min
        too_old = age_max is not None and age > age_max
        result["age"] = VIOLATION if too_young or too_old else OK

    term = shortest_term(product)
    stay = remaining_stay_months(profile, as_of)
    result["stayTerm"] = UNKNOWN if term is None else (OK if term <= stay else VIOLATION)

    # 여유자금은 월 흐름이라 적금의 월 최소액과만 비교한다. 예금(목돈)은 프로필에 목돈 값이
    # 없으므로 최소 가입액 조건을 적용하지 않는다(위반도 판정 불가도 아님).
    if kind == "SAVINGS":
        minimum = value(product, "monthlyMinKrw")
        result["spare"] = (
            UNKNOWN if minimum is None else (OK if minimum <= profile["spareKrw"] else VIOLATION)
        )

    if "maxTermMonths" in conditions:
        result["requestTerm"] = (
            UNKNOWN if term is None else (OK if term <= conditions["maxTermMonths"] else VIOLATION)
        )

    if "monthlyKrw" in conditions:
        amount = conditions["monthlyKrw"]
        low = value(product, "monthlyMinKrw")
        high = _upper(value(product, "monthlyMaxKrw"), no_limit(product, "monthlyMaxKrw"), amount)
        if amount > profile["spareKrw"]:
            result["requestMonthly"] = VIOLATION
        elif low is None or high == UNKNOWN:
            result["requestMonthly"] = UNKNOWN
        else:
            result["requestMonthly"] = VIOLATION if amount < low or high == VIOLATION else OK

    if "savingType" in conditions:
        saving = value(product, "savingType")
        result["savingType"] = (
            UNKNOWN if saving is None else (OK if saving == conditions["savingType"] else VIOLATION)
        )
    return result


def is_gold_candidate(product: Product, profile: Profile, request: Request, as_of: date) -> bool:
    checks = check_product(product, profile, request, as_of)
    if VIOLATION in checks.values():
        return False
    if any(checks.get(name) == UNKNOWN for name in REQUEST_CONDITIONS):
        return False
    for field, wanted in request.get("goldRequires", {}).items():
        if value(product, field) != wanted:
            return False
    return True


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def catalog_index(catalog: Mapping[str, Any]) -> dict[str, Product]:
    return {p["productId"]: p for p in catalog["products"]}


def expand_questions(qset: Mapping[str, Any]) -> list[dict[str, Any]]:
    """질문 세트의 문항에 프로필·요청 본문을 붙인다."""
    profiles = {p["id"]: p for p in qset["profiles"]}
    requests = {r["id"]: r for r in qset["requests"]}
    return [
        {**q, "profile": profiles[q["profileId"]], "request": requests[q["requestId"]]}
        for q in qset["questions"]
    ]


def gold_candidates(catalog: Mapping[str, Any], qset: Mapping[str, Any]) -> list[dict[str, Any]]:
    """문항마다 조건표로 계산한 gold 후보와, 판정 불가 때문에 빠진 상품."""
    as_of = date.fromisoformat(qset["asOf"])
    rows = []
    for q in expand_questions(qset):
        candidates, unknown_request = [], []
        for product in catalog["products"]:
            if is_gold_candidate(product, q["profile"], q["request"], as_of):
                candidates.append(product["productId"])
                continue
            checks = check_product(product, q["profile"], q["request"], as_of)
            if VIOLATION not in checks.values() and any(
                checks.get(name) == UNKNOWN for name in REQUEST_CONDITIONS
            ):
                unknown_request.append(product["productId"])
        rows.append({"id": q["id"], "candidates": candidates, "unknownRequest": unknown_request})
    return rows


def as_recommendation(result: Recommendation | Sequence[str]) -> Recommendation:
    """추천 결과를 {"recommended", "pool", "reason"} 형태로 맞춘다(목록만 오면 추천 목록)."""
    if isinstance(result, Mapping):
        return result
    return {"recommended": list(result)}


def score_question(
    question: Mapping[str, Any],
    recommended: Recommendation | Sequence[str],
    products: Mapping[str, Product],
    as_of: date,
) -> dict[str, Any]:
    """문항 하나의 추천(상위 k, 후보 풀, 기권 이유)을 채점한다."""
    result = as_recommendation(recommended)
    top = list(result["recommended"])[:TOP_K]
    pool = result.get("pool")
    reason = result.get("reason")
    gold = list(question["gold"])
    expected_reason = question.get("expectedReason")
    checks = {
        pid: check_product(products.get(pid), question["profile"], question["request"], as_of)
        for pid in top
    }
    violations = Counter(
        name
        for result in checks.values()
        for name, verdict in result.items()
        if verdict == VIOLATION
    )
    unknowns = Counter(
        name for result in checks.values() for name, verdict in result.items() if verdict == UNKNOWN
    )
    hits = [pid for pid in top if pid in gold]
    row: dict[str, Any] = {
        "id": question["id"],
        "profileId": question["profileId"],
        "requestId": question["requestId"],
        "query": question["request"]["query"],
        "gold": gold,
        "recommended": top,
        "checks": checks,
        "violatingProducts": sum(1 for r in checks.values() if VIOLATION in r.values()),
        "violations": sum(violations.values()),
        "violationsBy": dict(violations),
        "unknownsBy": dict(unknowns),
        "goldHits": hits,
        "recall@5": len(hits) / min(len(gold), TOP_K) if gold else None,
        "emptyGold": not gold,
        "abstained": not top,
        "falseAbstain": bool(gold) and not top,
        "pool": None if pool is None else list(pool),
        "goldNotInPool": None if pool is None else [g for g in gold if g not in pool],
        "reason": reason,
        "expectedReason": expected_reason,
        "reasonMatch": (reason == expected_reason) if not gold else None,
    }
    if "toolArgs" in result:
        row["toolArgs"] = result["toolArgs"]
    return row


def summarize(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    with_gold = [r for r in rows if not r["emptyGold"]]
    empty = [r for r in rows if r["emptyGold"]]
    recommended = sum(len(r["recommended"]) for r in rows)
    violations: Counter[str] = Counter()
    unknowns: Counter[str] = Counter()
    for r in rows:
        violations.update(r["violationsBy"])
        unknowns.update(r["unknownsBy"])
    return {
        "questions": len(rows),
        "recommended": recommended,
        "violatingProducts": sum(r["violatingProducts"] for r in rows),
        "violations": sum(r["violations"] for r in rows),
        "questionsWithViolation": sum(1 for r in rows if r["violatingProducts"]),
        "violationsBy": {name: violations.get(name, 0) for name in CONDITIONS},
        "unknownsBy": {name: unknowns.get(name, 0) for name in CONDITIONS},
        "goldQuestions": len(with_gold),
        "recall@5": statistics.mean(r["recall@5"] for r in with_gold) if with_gold else None,
        "emptyGoldQuestions": len(empty),
        "emptyGoldAbstained": sum(1 for r in empty if r["abstained"]),
        "emptyGoldAbstainRate": (
            sum(1 for r in empty if r["abstained"]) / len(empty) if empty else None
        ),
        "falseAbstain": sum(1 for r in with_gold if r["abstained"]),
        "reasonMatch": sum(1 for r in empty if r.get("reasonMatch")),
        "goldNotInPool": (
            None
            if any(r.get("goldNotInPool") is None for r in rows)
            else sum(len(r["goldNotInPool"]) for r in rows)
        ),
    }


def regressions(base: Sequence[Mapping[str, Any]], rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """gold가 있는 문항 중 기준(base)보다 gold 적중 수가 줄어든 문항 id."""
    before = {r["id"]: len(r["goldHits"]) for r in base}
    return [r["id"] for r in rows if not r["emptyGold"] and len(r["goldHits"]) < before[r["id"]]]


def evaluate(
    catalog: Mapping[str, Any],
    qset: Mapping[str, Any],
    recommend: Callable[[Mapping[str, Any]], Recommendation | Sequence[str]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """recommend(문항) → 추천(상품 id 목록 또는 Recommendation)으로 문항별 결과·요약을 만든다."""
    as_of = date.fromisoformat(qset["asOf"])
    products = catalog_index(catalog)
    rows = [score_question(q, recommend(q), products, as_of) for q in expand_questions(qset)]
    return rows, summarize(rows)


def rank_products(
    fused: Sequence[tuple[str, float]], excluded: set[str], k: int = TOP_K
) -> list[str]:
    """(문서 id, 점수) 청크 순위에서 excluded 문서를 빼고 문서별 최고 점수로 상위 k 문서를 고른다.

    동점은 먼저 나온 청크 순서(입력 순서)로 정한다.
    """
    best: dict[str, tuple[float, int]] = {}
    for order, (doc_id, score) in enumerate(fused):
        if doc_id in excluded:
            continue
        if doc_id not in best or score > best[doc_id][0]:
            best[doc_id] = (score, best.get(doc_id, (score, order))[1])
    ranked = sorted(best.items(), key=lambda item: (-item[1][0], item[1][1]))
    return [doc_id for doc_id, _ in ranked[:k]]


# ②③: 조건은 챗봇 도구 인자와 같은 형태로 받는다.
# 오프라인 평가에서는 질문 세트의 요청 조건을 이 형태로 바꿔 넣는다.
REASON_SURPLUS = "REQUEST_EXCEEDS_SURPLUS"
REASON_STAY = "STAY_TOO_SHORT"
REASON_NONE = "NO_MATCHING_PRODUCT"


def tool_args(request: Request) -> dict[str, Any]:
    """질문 세트의 요청 조건 → 챗봇 도구 인자 형태."""
    conditions = request.get("conditions", {})
    return {
        "productType": conditions.get("kind"),
        "maxTermMonths": conditions.get("maxTermMonths"),
        "monthlyAmountKrw": conditions.get("monthlyKrw"),
        "accrual": conditions.get("savingType"),
        "requireForeignerExplicit": (
            request.get("goldRequires", {}).get("foreignerEligible") == "explicit"
        ),
    }


def request_from_args(args: Mapping[str, Any]) -> dict[str, Any]:
    """도구 인자 → check_product가 읽는 요청 조건."""
    pairs = [
        ("kind", args.get("productType")),
        ("maxTermMonths", args.get("maxTermMonths")),
        ("monthlyKrw", args.get("monthlyAmountKrw")),
        ("savingType", args.get("accrual")),
    ]
    return {"conditions": {name: v for name, v in pairs if v is not None}}


def passes(
    product: Product,
    profile: Profile,
    args: Mapping[str, Any],
    as_of: date,
    ignore: frozenset[str] = frozenset(),
) -> bool:
    """위반 없음 + 요청 조건 판정 불가 없음 + (요청 시) 외국인 명시. ignore 조건은 보지 않는다."""
    checks = check_product(product, profile, request_from_args(args), as_of)
    checks = {name: verdict for name, verdict in checks.items() if name not in ignore}
    if VIOLATION in checks.values():
        return False
    if any(checks.get(name) == UNKNOWN for name in REQUEST_CONDITIONS):
        return False
    if args.get("requireForeignerExplicit") and value(product, "foreignerEligible") != "explicit":
        return False
    return True


def abstain_reason(
    pool: Sequence[str],
    products: Mapping[str, Product],
    profile: Profile,
    args: Mapping[str, Any],
    as_of: date,
) -> str:
    """남는 상품이 없을 때의 이유 코드(9-1 순서)."""
    amount = args.get("monthlyAmountKrw")
    if amount is not None and amount > profile["spareKrw"]:
        return REASON_SURPLUS
    without_stay = frozenset({"stayTerm"})
    if any(passes(products[pid], profile, args, as_of, without_stay) for pid in pool):
        return REASON_STAY
    return REASON_NONE


def recommend_filtered(
    pool: Sequence[str],
    products: Mapping[str, Product],
    profile: Profile,
    args: Mapping[str, Any],
    as_of: date,
    *,
    explicit_first: bool,
    with_reason: bool,
    k: int = TOP_K,
) -> Recommendation:
    """② / ③: 후보 풀(검색 점수 순)에서 조건을 통과한 상품만 남겨 상위 k를 고른다.

    pool은 조건표에 있는 상품만 담는다(약관은 미리 뺀다). explicit_first면
    foreignerEligible=explicit 상품을 먼저 두고(안에서는 검색 순서 유지),
    with_reason이면 빈 목록일 때 이유 코드를 붙인다.
    """
    kept = [pid for pid in pool if passes(products[pid], profile, args, as_of)]
    if explicit_first:
        kept.sort(key=lambda pid: value(products[pid], "foreignerEligible") != "explicit")
    top = kept[:k]
    reason = (
        abstain_reason(pool, products, profile, args, as_of) if with_reason and not top else None
    )
    return {"recommended": top, "pool": list(pool), "reason": reason, "toolArgs": dict(args)}


def _fmt(x: float | None) -> str:
    return "–" if x is None else f"{x:.3f}"


def print_gold_table(catalog: Mapping[str, Any], qset: Mapping[str, Any]) -> None:
    names = {p["productId"]: value(p, "nameKo") for p in catalog["products"]}
    fixed = {q["id"]: q["gold"] for q in qset["questions"]}
    print(
        "| 문항 | 프로필 | 요청 | gold 후보 | 요청 조건 판정 불가로 빠짐 | 질문 세트 gold와 같음 |"
    )
    print("|---|---|---|---|---|---|")
    expanded = {q["id"]: q for q in expand_questions(qset)}
    for row in gold_candidates(catalog, qset):
        q = expanded[row["id"]]
        cand = ", ".join(names[p] for p in row["candidates"]) or "(없음)"
        unknown = ", ".join(names[p] for p in row["unknownRequest"]) or "–"
        same = "예" if sorted(row["candidates"]) == sorted(fixed[row["id"]]) else "아니오"
        cells = [row["id"], q["profileId"], q["request"]["query"], cand, unknown, same]
        print("| " + " | ".join(cells) + " |")


def with_gold_candidates(catalog: Mapping[str, Any], qset: Mapping[str, Any]) -> dict[str, Any]:
    """문항마다 gold를 조건표 gold 후보로 채운 질문 세트 사본. 사람이 확인하기 전이라 미확정."""
    candidates = {row["id"]: row["candidates"] for row in gold_candidates(catalog, qset)}
    questions = [{**q, "gold": candidates[q["id"]]} for q in qset["questions"]]
    return {**qset, "goldConfirmed": False, "questions": questions}


def write_gold_candidates(catalog: Mapping[str, Any], qset: Mapping[str, Any], output: Path) -> int:
    """gold 후보를 채운 질문 세트를 새 파일로 쓰고 문항 수를 돌려준다(있는 파일은 덮어쓰지 않음)."""
    filled = with_gold_candidates(catalog, qset)
    text = json.dumps(filled, ensure_ascii=False, indent=2) + "\n"
    with output.open("x", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return len(filled["questions"])


METHODS = {
    "1": "③ 검색(dense+BM25 RRF) → 약관 청크 제외 → 상품별 최고 점수 상위 5, 필터 없음",
    "2": "① 후보 풀 → 조건표 필터(위반·요청 조건 판정 불가·외국인 명시 요청) → 검색 점수 상위 5",
    "3": "② + foreignerEligible explicit 우선 + 남는 상품이 없으면 이유 코드",
}


def make_recommender(
    method: str,
    search: Callable[[str], list[str]],
    products: Mapping[str, Product],
    as_of: date,
) -> Callable[[Mapping[str, Any]], Recommendation]:
    """search(검색어) → 조건표 상품만 담은 후보 풀(검색 점수 순)을 받아 방식별 추천기를 만든다."""

    def recommend(question: Mapping[str, Any]) -> Recommendation:
        pool = search(question["request"]["query"])
        if method == "1":
            return {"recommended": pool[:TOP_K], "pool": pool}
        return recommend_filtered(
            pool,
            products,
            question["profile"],
            tool_args(question["request"]),
            as_of,
            explicit_first=method == "3",
            with_reason=method == "3",
        )

    return recommend


def comparison(
    results: Mapping[str, tuple[list[dict[str, Any]], dict[str, Any]]],
) -> list[dict[str, Any]]:
    """방식별 9-2 기준 표. 문항별 후퇴는 ① 대비."""
    base_rows = results["1"][0] if "1" in results else None
    table = []
    for method, (rows, summary) in results.items():
        back = regressions(base_rows, rows) if base_rows is not None else None
        table.append(
            {
                "method": method,
                "recall@5": summary["recall@5"],
                "regressions": back,
                "abstain": f"{summary['emptyGoldAbstained']}/{summary['emptyGoldQuestions']}",
                "falseAbstain": f"{summary['falseAbstain']}/{summary['goldQuestions']}",
                "reasonMatch": f"{summary['reasonMatch']}/{summary['emptyGoldQuestions']}",
                "goldNotInPool": summary["goldNotInPool"],
                "violatingProducts": summary["violatingProducts"],
            }
        )
    return table


def run_methods(args: argparse.Namespace, methods: Sequence[str]) -> int:
    from app.rag.embeddings import BgeM3Embedder
    from app.rag.experiments.product_corpus import load_corpus
    from app.rag.experiments.product_eval import (
        VARIANTS,
        EmbeddingCache,
        build_chunks,
        build_retriever,
        texts_sha256,
    )

    catalog = load_json(args.catalog)
    qset = load_json(args.questions)
    products = catalog_index(catalog)
    as_of = date.fromisoformat(qset["asOf"])
    documents = load_corpus(args.corpus)
    terms = {d.doc_id for d in documents if d.kind == "terms"}
    if terms != set(catalog["termsDocuments"]):
        print(f"FAILED: terms documents {sorted(terms)} != catalog {catalog['termsDocuments']}")
        return 1
    if {d.doc_id for d in documents} - terms != set(products):
        print("FAILED: product documents differ from the catalog")
        return 1
    variant = VARIANTS["3"]
    embedder = BgeM3Embedder(
        args.model, revision=args.revision, device=args.device, batch_size=args.batch_size
    )
    embedder.load()
    # #47과 같은 청크 목록(prefix 없음, 토큰 수는 BGE-M3 토크나이저).
    chunks = build_chunks(documents, variant, embedder.count_tokens)
    cache = EmbeddingCache(args.cache_dir, args.model, args.revision)
    retriever, index_info = build_retriever(variant, embedder, chunks, documents, cache)

    def search(query: str) -> list[str]:
        # dense 20 + BM25 20을 RRF로 합친 전체 후보(최대 40 청크) → 약관 제외 → 상품 순위(후보 풀).
        fused = retriever.search(query, len(chunks))
        return rank_products([(chunks[i].doc_id, s) for i, s in fused], terms, k=len(products))

    started = datetime.now(UTC)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, tuple[list[dict[str, Any]], dict[str, Any]]] = {}
    for method in methods:
        rows, summary = evaluate(catalog, qset, make_recommender(method, search, products, as_of))
        results[method] = (rows, summary)
        meta = {
            "type": "conditions",
            "method": method,
            "description": METHODS[method],
            "startedAt": started.isoformat(timespec="seconds"),
            "model": args.model,
            "revision": args.revision,
            "chunks": len(chunks),
            "chunkTextSha256": texts_sha256([c.text for c in chunks]),
            "index": index_info,
            "catalog": args.catalog.as_posix(),
            "questions": args.questions.as_posix(),
            "summary": summary,
        }
        jsonl = args.output_dir / f"product-recommend-{method}-{stamp}.jsonl"
        with jsonl.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(meta, ensure_ascii=False) + "\n")
            for row in rows:
                fh.write(json.dumps({"type": "question", **row}, ensure_ascii=False) + "\n")
        print(f"wrote {jsonl}")
    # 문항 내용은 출력하지 않는다(집계만).
    print(json.dumps({m: r[1] for m, r in results.items()}, ensure_ascii=False, indent=2))
    print(json.dumps(comparison(results), ensure_ascii=False, indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    gold = sub.add_parser("gold", help="조건표로 gold 후보 표를 출력(모델 없음)")
    base = sub.add_parser("baseline", help="기준선 ①만 돌려 문항별 jsonl을 쓴다")
    run = sub.add_parser("run", help="①②③을 같은 검색으로 돌려 방식별 jsonl과 비교표를 쓴다")
    run.add_argument("--methods", default="1,2,3", help="쉼표로 구분한 방식(1, 2, 3)")
    for p in (gold, base, run):
        p.add_argument("--catalog", type=Path, default=CATALOG_PATH)
        p.add_argument("--questions", type=Path, default=QUESTIONS_PATH)
    gold.add_argument(
        "--write",
        type=Path,
        help="gold를 후보로 채운 질문 세트를 새 파일로 쓴다(goldConfirmed=false, 덮어쓰지 않음)",
    )
    for p in (base, run):
        p.add_argument("--corpus", type=Path, required=True, help="상품설명서·약관 PDF 폴더")
        p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
        p.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
        p.add_argument("--model", default="BAAI/bge-m3")
        p.add_argument("--revision", required=True, help="BGE-M3 HF 커밋 해시(#47과 같은 값)")
        p.add_argument("--device", default="cpu")
        p.add_argument("--batch-size", type=int, default=8)
    args = parser.parse_args(argv)
    if args.command == "gold":
        catalog, qset = load_json(args.catalog), load_json(args.questions)
        print_gold_table(catalog, qset)
        if args.write is not None:
            if args.write.exists():
                print(f"FAILED: {args.write} already exists (not overwritten)")
                return 1
            count = write_gold_candidates(catalog, qset, args.write)
            # 파일 내용은 출력하지 않는다(경로와 문항 수만).
            print(f"\nwrote {args.write} ({count} questions, goldConfirmed=false)")
        return 0
    if args.command == "baseline":
        return run_methods(args, ["1"])
    methods = [m.strip() for m in args.methods.split(",") if m.strip()]
    unknown = [m for m in methods if m not in METHODS]
    if unknown or not methods:
        print(f"FAILED: unknown methods {unknown}; choose from {list(METHODS)}")
        return 1
    return run_methods(args, methods)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    raise SystemExit(main())

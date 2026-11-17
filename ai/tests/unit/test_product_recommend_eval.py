"""scripts/product_recommend_eval.py: 조건 판정, gold 후보, 상품 순위, 지표(모델·PDF 없음)."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.products import recommender as REC

pytestmark = pytest.mark.unit

AI_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = AI_ROOT / "scripts"


def _script() -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "product_recommend_eval", SCRIPTS / "product_recommend_eval.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EV = _script()
AS_OF = date(2026, 11, 16)
RATE_FIELDS = {"baseRate", "maxRate", "feature"}
PROFILE = {"id": "P", "visa": "E-9", "stayExpiry": "2027-10-04", "spareKrw": 675317, "age": 30}


def _v(value: Any, **extra: Any) -> dict[str, Any]:
    return {"value": value, "source": None, **extra}


def _savings(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "productId": "doc-s",
        "recommendable": True,
        "kind": _v("SAVINGS"),
        "foreignerEligible": _v("inferred"),
        "visaTypes": _v(None),
        "ageMin": _v(None),
        "ageMax": _v(None),
        "termOptions": _v(None),
        "termMonthsMin": _v(6),
        "termMonthsMax": _v(6),
        "monthlyMinKrw": _v(1000),
        "monthlyMaxKrw": _v(300000),
        "depositMinKrw": _v(None),
        "depositMaxKrw": _v(None),
        "savingType": _v("자유"),
    }
    base.update(fields)
    return base


def _request(query: str = "q", **conditions: Any) -> dict[str, Any]:
    return {"id": "R", "query": query, "conditions": conditions}


@pytest.mark.parametrize(
    ("end", "months"),
    [("2027-10-04", 10), ("2027-05-16", 6), ("2028-11-16", 24), ("2026-11-15", 0)],
)
def test_months_between_counts_full_months(end: str, months: int) -> None:
    assert REC.months_between(AS_OF, date.fromisoformat(end)) == months


def test_profile_checks() -> None:
    checks = REC.check_product(_savings(), PROFILE, _request(kind="SAVINGS"), AS_OF)
    assert checks["kind"] == "ok"
    assert checks["foreigner"] == "ok"  # inferred는 위반이 아니다
    assert checks["visa"] == "unknown"
    assert checks["age"] == "unknown"
    assert checks["stayTerm"] == "ok"
    assert checks["spare"] == "ok"
    youth = _savings(ageMin=_v(18), ageMax=_v(39), termMonthsMin=_v(12))
    old = {**PROFILE, "age": 40}
    checks = REC.check_product(youth, old, _request(kind="DEPOSIT"), AS_OF)
    assert checks["age"] == "violation"
    assert checks["stayTerm"] == "violation"  # 12개월 > 남은 체류 10개월
    assert checks["kind"] == "violation"
    assert (
        REC.check_product(_savings(foreignerEligible=_v(False)), PROFILE, _request(), AS_OF)[
            "foreigner"
        ]
        == "violation"
    )
    assert (
        REC.check_product(_savings(foreignerEligible=_v(None)), PROFILE, _request(), AS_OF)[
            "foreigner"
        ]
        == "unknown"
    )


def test_term_options_use_the_shortest_choice() -> None:
    product = _savings(termOptions=_v([6, 12]), termMonthsMin=_v(6), termMonthsMax=_v(12))
    short_stay = {**PROFILE, "stayExpiry": "2027-02-16"}  # 3개월
    assert REC.shortest_term(product) == 6
    assert REC.check_product(product, short_stay, _request(), AS_OF)["stayTerm"] == "violation"


def test_request_monthly_and_no_limit() -> None:
    request = _request(kind="SAVINGS", monthlyKrw=300000)
    assert REC.check_product(_savings(), PROFILE, request, AS_OF)["requestMonthly"] == "ok"
    small = _savings(monthlyMaxKrw=_v(200000))
    assert REC.check_product(small, PROFILE, request, AS_OF)["requestMonthly"] == "violation"
    unlimited = _savings(monthlyMaxKrw=_v(None, noLimit=True))
    assert REC.check_product(unlimited, PROFILE, request, AS_OF)["requestMonthly"] == "ok"
    missing = _savings(monthlyMinKrw=_v(None), monthlyMaxKrw=_v(None))
    assert REC.check_product(missing, PROFILE, request, AS_OF)["requestMonthly"] == "unknown"
    # 요청액이 여유자금보다 크면 어떤 상품이든 위반이다.
    poor = {**PROFILE, "spareKrw": 200000}
    assert REC.check_product(_savings(), poor, request, AS_OF)["requestMonthly"] == "violation"


def test_terms_document_and_not_recommendable_are_violations() -> None:
    assert REC.check_product(None, PROFILE, _request(), AS_OF) == {"recommendable": "violation"}
    gift = _savings(recommendable=False)
    assert REC.check_product(gift, PROFILE, _request(), AS_OF)["recommendable"] == "violation"


def test_gold_candidate_rules() -> None:
    request = _request(kind="SAVINGS", monthlyKrw=300000)
    assert EV.is_gold_candidate(_savings(), PROFILE, request, AS_OF)
    # 요청 조건이 판정 불가면 후보에서 뺀다. 프로필 조건(나이·비자)의 판정 불가는 그대로 둔다.
    missing = _savings(monthlyMinKrw=_v(None), monthlyMaxKrw=_v(None))
    assert not EV.is_gold_candidate(missing, PROFILE, request, AS_OF)
    explicit_only = {**_request(), "goldRequires": {"foreignerEligible": "explicit"}}
    assert not EV.is_gold_candidate(_savings(), PROFILE, explicit_only, AS_OF)
    explicit = _savings(foreignerEligible=_v("explicit"))
    assert EV.is_gold_candidate(explicit, PROFILE, explicit_only, AS_OF)


def test_rank_products_drops_terms_and_keeps_best_chunk() -> None:
    fused = [("t", 0.9), ("a", 0.8), ("b", 0.7), ("a", 0.6), ("c", 0.5)]
    assert EV.rank_products(fused, {"t"}) == ["a", "b", "c"]
    assert EV.rank_products(fused, set(), k=2) == ["t", "a"]


def _qset() -> dict[str, Any]:
    return {
        "asOf": "2026-11-16",
        "profiles": [PROFILE],
        "requests": [_request("적금", kind="SAVINGS"), {**_request("x"), "id": "R2"}],
        "questions": [
            {"id": "P-R", "profileId": "P", "requestId": "R", "gold": ["doc-s", "doc-g"]},
            {"id": "P-R2", "profileId": "P", "requestId": "R2", "gold": []},
        ],
    }


def test_evaluate_scores_violations_recall_and_empty_gold() -> None:
    catalog = {
        "products": [
            _savings(),
            _savings(productId="doc-g"),
            _savings(productId="doc-d", kind=_v("DEPOSIT"), depositMinKrw=_v(3000000)),
        ]
    }
    picks = {"P-R": ["doc-d", "doc-s", "doc-terms"], "P-R2": []}
    rows, summary = EV.evaluate(catalog, _qset(), lambda q: picks[q["id"]])
    first = rows[0]
    assert first["recommended"] == ["doc-d", "doc-s", "doc-terms"]
    assert first["goldHits"] == ["doc-s"]
    assert first["recall@5"] == 0.5
    # doc-d: 종류 위반(예금 최소 가입액은 월 여유자금과 비교하지 않음), doc-terms: 추천 대상 아님
    assert first["violatingProducts"] == 2
    assert first["violationsBy"] == {"kind": 1, "recommendable": 1}
    assert "spare" not in first["checks"]["doc-d"]
    assert first["unknownsBy"]["visa"] == 2
    assert rows[1]["emptyGold"] and rows[1]["abstained"]
    assert summary["recall@5"] == 0.5
    assert summary["emptyGoldAbstainRate"] == 1.0
    assert summary["questionsWithViolation"] == 1


def test_recall_is_capped_at_five() -> None:
    question = {
        "id": "q", "profileId": "P", "requestId": "R", "profile": PROFILE,
        "request": _request(), "gold": [f"g{i}" for i in range(8)],
    }  # fmt: skip
    row = EV.score_question(question, [f"g{i}" for i in range(5)], {}, AS_OF)
    assert row["recall@5"] == 1.0


def test_repo_catalog_and_question_set_are_consistent() -> None:
    catalog = json.loads(EV.CATALOG_PATH.read_text(encoding="utf-8"))
    qset = json.loads(EV.QUESTIONS_PATH.read_text(encoding="utf-8"))
    products = EV.catalog_index(catalog)
    assert len(products) == 13
    assert sum(p["recommendable"] for p in products.values()) == 12
    assert not set(catalog["termsDocuments"]) & set(products)
    for product in products.values():
        for name, entry in product.items():
            if not isinstance(entry, dict):
                continue
            source = entry["source"]
            if entry["value"] is None and not entry.get("noLimit"):
                continue
            assert source is not None, (product["productId"], name)
            # 금리·특징(#84)은 원문 문장·표 행을 그대로 옮겨 30자 제한이 없다.
            if name not in RATE_FIELDS:
                assert len(source["quote"]) <= 30, (product["productId"], name)
    for product in products.values():
        base = product["baseRate"]
        assert base["value"] is not None and base["asOf"], product["productId"]
        assert base["asOf"].replace("-", ".") in (base.get("asOfSource") or base["source"])["quote"]
        assert product["feature"]["value"] == product["feature"]["source"]["quote"]
    assert sum(p["maxRate"]["value"] is not None for p in products.values()) == 6
    assert {v["foreignerEligible"]["value"] for v in products.values()} <= {
        "explicit", "inferred", None,
    }  # fmt: skip
    questions = qset["questions"]
    assert len(questions) == 30
    assert len({q["id"] for q in questions}) == 30
    for question in questions:
        assert set(question["gold"]) <= set(products)
        assert all(products[pid]["recommendable"] for pid in question["gold"])


def _gold_files(tmp_path: Path) -> tuple[Path, Path]:
    catalog = {
        "products": [
            _savings(nameKo=_v("적금 S")),
            _savings(productId="doc-d", nameKo=_v("예금 D"), kind=_v("DEPOSIT")),
        ]
    }
    qset = {**_qset(), "goldConfirmed": True}
    qset["questions"] = [{**q, "gold": ["stale"]} for q in qset["questions"]]
    catalog_path, questions_path = tmp_path / "catalog.json", tmp_path / "questions.json"
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    questions_path.write_text(json.dumps(qset, ensure_ascii=False), encoding="utf-8")
    return catalog_path, questions_path


def test_gold_write_fills_candidates_unconfirmed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    catalog_path, questions_path = _gold_files(tmp_path)
    output = tmp_path / "out.json"
    argv = ["gold", "--catalog", str(catalog_path), "--questions", str(questions_path)]
    assert EV.main([*argv, "--write", str(output)]) == 0
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["goldConfirmed"] is False
    # R은 적금 요청 → 적금만, R2는 조건 없음 → 둘 다.
    assert {q["id"]: q["gold"] for q in written["questions"]} == {
        "P-R": ["doc-s"],
        "P-R2": ["doc-s", "doc-d"],
    }
    original = json.loads(questions_path.read_text(encoding="utf-8"))
    assert original["goldConfirmed"] is True  # 입력 파일은 그대로
    assert {k: v for k, v in written.items() if k != "questions" and k != "goldConfirmed"} == {
        k: v for k, v in original.items() if k != "questions" and k != "goldConfirmed"
    }
    out = capsys.readouterr().out
    assert f"wrote {output} (2 questions, goldConfirmed=false)" in out
    assert '"gold"' not in out and "stale" not in out  # 파일 내용은 출력하지 않는다
    # 있는 파일은 덮어쓰지 않는다.
    assert EV.main([*argv, "--write", str(output)]) == 1
    assert json.loads(output.read_text(encoding="utf-8")) == written


def test_tool_args_and_request_round_trip() -> None:
    request = {
        "conditions": {"kind": "SAVINGS", "maxTermMonths": 3, "monthlyKrw": 300000},
        "goldRequires": {"foreignerEligible": "explicit"},
    }
    args = EV.tool_args(request)
    assert args == {
        "productType": "SAVINGS",
        "maxTermMonths": 3,
        "monthlyAmountKrw": 300000,
        "accrual": None,
        "requireForeignerExplicit": True,
    }
    assert REC.request_from_args(args) == {"conditions": request["conditions"]}


def test_passes_keeps_profile_unknowns_and_drops_request_unknowns() -> None:
    args = EV.tool_args(_request(kind="SAVINGS"))
    # 나이·비자 판정 불가(프로필 조건)는 남긴다.
    assert REC.passes(_savings(), PROFILE, args, AS_OF)
    monthly = EV.tool_args(_request(kind="SAVINGS", monthlyKrw=300000))
    missing = _savings(monthlyMinKrw=_v(None), monthlyMaxKrw=_v(None))
    assert not REC.passes(missing, PROFILE, monthly, AS_OF)  # 요청 조건 판정 불가는 뺀다
    explicit = {**args, "requireForeignerExplicit": True}
    assert not REC.passes(_savings(), PROFILE, explicit, AS_OF)
    assert REC.passes(_savings(foreignerEligible=_v("explicit")), PROFILE, explicit, AS_OF)
    long_term = _savings(termMonthsMin=_v(12))
    assert not REC.passes(long_term, PROFILE, args, AS_OF)
    assert REC.passes(long_term, PROFILE, args, AS_OF, frozenset({"stayTerm"}))


def _products() -> dict[str, Any]:
    return {
        "a": _savings(productId="a"),
        "b": _savings(productId="b", foreignerEligible=_v("explicit")),
        "long": _savings(productId="long", termMonthsMin=_v(12), foreignerEligible=_v("explicit")),
        "gift": _savings(productId="gift", recommendable=False),
    }


def test_recommend_filtered_orders_and_explains() -> None:
    products = _products()
    pool = ["gift", "a", "long", "b"]
    args = EV.tool_args(_request(kind="SAVINGS"))
    two = REC.recommend_filtered(
        pool, products, PROFILE, args, AS_OF, explicit_first=False, with_reason=False
    )
    assert two["recommended"] == ["a", "b"] and two["reason"] is None
    three = REC.recommend_filtered(
        pool, products, PROFILE, args, AS_OF, explicit_first=True, with_reason=True
    )
    assert three["recommended"] == ["b", "a"]  # explicit 먼저
    assert three["pool"] == pool and three["toolArgs"] == args

    def reason(args: dict[str, Any], profile: dict[str, Any] = PROFILE) -> Any:
        return REC.recommend_filtered(
            pool, products, profile, args, AS_OF, explicit_first=True, with_reason=True
        )

    explicit_long = {**args, "requireForeignerExplicit": True}
    short_stay = {**PROFILE, "stayExpiry": "2027-04-16"}  # 5개월: b(6개월)도 빠짐
    result = reason(explicit_long, short_stay)
    assert result["recommended"] == [] and result["reason"] == REC.REASON_STAY
    poor = {**PROFILE, "spareKrw": 200000}
    monthly = EV.tool_args(_request(kind="SAVINGS", monthlyKrw=300000))
    assert reason(monthly, poor)["reason"] == REC.REASON_SURPLUS
    deposit = EV.tool_args(_request(kind="DEPOSIT"))
    assert reason(deposit)["reason"] == REC.REASON_NONE


def test_regressions_and_summary_fields() -> None:
    catalog = {"products": [_savings(), _savings(productId="doc-g")]}
    qset = _qset()
    qset["questions"][1]["expectedReason"] = REC.REASON_NONE
    base, _ = EV.evaluate(catalog, qset, lambda q: ["doc-s", "doc-g"] if q["id"] == "P-R" else [])
    picks = {
        "P-R": {"recommended": ["doc-s"], "pool": ["doc-s"]},
        "P-R2": {"recommended": [], "pool": [], "reason": REC.REASON_NONE},
    }
    rows, summary = EV.evaluate(catalog, qset, lambda q: picks[q["id"]])
    assert EV.regressions(base, rows) == ["P-R"]  # 적중 2 → 1
    assert rows[0]["goldNotInPool"] == ["doc-g"]
    assert summary["goldNotInPool"] == 1
    assert summary["falseAbstain"] == 0 and summary["reasonMatch"] == 1
    assert rows[1]["reasonMatch"] is True and rows[0]["reasonMatch"] is None


def test_filters_on_the_repo_set_have_no_judgeable_violations() -> None:
    # 9-2: 필터와 채점이 같은 조건표·규칙을 쓰므로 위반 0은 구조상 보장이다(후보 풀 = 상품 전체).
    catalog = json.loads(EV.CATALOG_PATH.read_text(encoding="utf-8"))
    qset = json.loads(EV.QUESTIONS_PATH.read_text(encoding="utf-8"))
    products = EV.catalog_index(catalog)
    pool = list(products)
    for method in ("2", "3"):
        recommend = EV.make_recommender(method, lambda _: pool, products, AS_OF)
        rows, summary = EV.evaluate(catalog, qset, recommend)
        assert summary["violatingProducts"] == 0
        assert summary["emptyGoldAbstained"] == summary["emptyGoldQuestions"] == 4
        assert summary["falseAbstain"] == 0
    assert summary["reasonMatch"] == 4  # ③만 이유 코드를 붙인다


# --- 프로필 값이 없을 때(#65: 생년월일·비자 프로필·분석 결과가 없는 사용자) -------------------


def test_missing_profile_values_are_unknown_not_violations() -> None:
    adult = _savings(ageMin=_v(19), visaTypes=_v(["E-9"]), termMonthsMin=_v(24))
    empty = {**PROFILE, "visa": None, "age": None, "stayExpiry": None, "spareKrw": None}
    checks = REC.check_product(adult, empty, _request(monthlyKrw=900_000), AS_OF)
    assert checks["visa"] == checks["age"] == checks["stayTerm"] == checks["spare"] == "unknown"
    assert checks["requestMonthly"] == "violation"  # 상품 월 상한(300,000)은 그대로 본다
    assert REC.remaining_stay_months(empty, AS_OF) is None


def test_missing_profile_values_do_not_give_their_abstain_reason() -> None:
    long_term = _savings(termMonthsMin=_v(36))
    products = {"doc-s": long_term}
    no_spare = {**PROFILE, "spareKrw": None}
    args = {"productType": "SAVINGS", "monthlyAmountKrw": 900_000}
    assert REC.abstain_reason(["doc-s"], products, no_spare, args, AS_OF) == REC.REASON_NONE
    no_stay = {**PROFILE, "stayExpiry": "2026-12-31"}
    assert REC.abstain_reason(["doc-s"], products, no_stay, {}, AS_OF) == REC.REASON_STAY
    unknown_stay = {**PROFILE, "stayExpiry": None}
    # 체류 만료일이 없으면 기간 조건은 판정 불가(제외하지 않음)라 상품이 남는다.
    assert REC.passes(long_term, unknown_stay, {}, AS_OF)


def test_search_order_puts_unsearched_products_after_in_catalog_order() -> None:
    from app.products.search import search_order

    assert search_order(["c", "x", "a"], ["a", "b", "c", "d"]) == ["c", "a", "b", "d"]

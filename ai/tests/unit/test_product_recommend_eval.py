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
    assert EV.months_between(AS_OF, date.fromisoformat(end)) == months


def test_profile_checks() -> None:
    checks = EV.check_product(_savings(), PROFILE, _request(kind="SAVINGS"), AS_OF)
    assert checks["kind"] == "ok"
    assert checks["foreigner"] == "ok"  # inferred는 위반이 아니다
    assert checks["visa"] == "unknown"
    assert checks["age"] == "unknown"
    assert checks["stayTerm"] == "ok"
    assert checks["spare"] == "ok"
    youth = _savings(ageMin=_v(18), ageMax=_v(39), termMonthsMin=_v(12))
    old = {**PROFILE, "age": 40}
    checks = EV.check_product(youth, old, _request(kind="DEPOSIT"), AS_OF)
    assert checks["age"] == "violation"
    assert checks["stayTerm"] == "violation"  # 12개월 > 남은 체류 10개월
    assert checks["kind"] == "violation"
    assert (
        EV.check_product(_savings(foreignerEligible=_v(False)), PROFILE, _request(), AS_OF)[
            "foreigner"
        ]
        == "violation"
    )
    assert (
        EV.check_product(_savings(foreignerEligible=_v(None)), PROFILE, _request(), AS_OF)[
            "foreigner"
        ]
        == "unknown"
    )


def test_term_options_use_the_shortest_choice() -> None:
    product = _savings(termOptions=_v([6, 12]), termMonthsMin=_v(6), termMonthsMax=_v(12))
    short_stay = {**PROFILE, "stayExpiry": "2027-02-16"}  # 3개월
    assert EV.shortest_term(product) == 6
    assert EV.check_product(product, short_stay, _request(), AS_OF)["stayTerm"] == "violation"


def test_request_monthly_and_no_limit() -> None:
    request = _request(kind="SAVINGS", monthlyKrw=300000)
    assert EV.check_product(_savings(), PROFILE, request, AS_OF)["requestMonthly"] == "ok"
    small = _savings(monthlyMaxKrw=_v(200000))
    assert EV.check_product(small, PROFILE, request, AS_OF)["requestMonthly"] == "violation"
    unlimited = _savings(monthlyMaxKrw=_v(None, noLimit=True))
    assert EV.check_product(unlimited, PROFILE, request, AS_OF)["requestMonthly"] == "ok"
    missing = _savings(monthlyMinKrw=_v(None), monthlyMaxKrw=_v(None))
    assert EV.check_product(missing, PROFILE, request, AS_OF)["requestMonthly"] == "unknown"
    # 요청액이 여유자금보다 크면 어떤 상품이든 위반이다.
    poor = {**PROFILE, "spareKrw": 200000}
    assert EV.check_product(_savings(), poor, request, AS_OF)["requestMonthly"] == "violation"


def test_terms_document_and_not_recommendable_are_violations() -> None:
    assert EV.check_product(None, PROFILE, _request(), AS_OF) == {"recommendable": "violation"}
    gift = _savings(recommendable=False)
    assert EV.check_product(gift, PROFILE, _request(), AS_OF)["recommendable"] == "violation"


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
            assert len(source["quote"]) <= 30, (product["productId"], name)
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

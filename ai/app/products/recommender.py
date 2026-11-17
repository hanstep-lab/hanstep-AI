"""금융상품 추천기: 조건표로 상품을 판정하고 조건을 통과한 상품만 고른다(이슈 #56, #65).

평가 스크립트(scripts/product_recommend_eval.py)와 챗봇 도구가 같은 코드를 쓴다.
판정 규칙: scripts/product_recommend_eval.py 모듈 설명,
specs/rag/product_recommend_v1_results.md 4절.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

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


def remaining_stay_months(profile: Profile, as_of: date) -> int | None:
    """남은 체류 개월. 프로필에 체류 만료일이 없으면 None(판정 불가)."""
    expiry = profile.get("stayExpiry")
    return None if expiry is None else months_between(as_of, date.fromisoformat(expiry))


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
    """상품 하나를 프로필·요청 조건으로 판정한다. product가 None이면 조건표에 없는 문서(약관).

    프로필 값(visa·age·stayExpiry·spareKrw)이 None이면 그 조건은 판정 불가다
    (#65, 생년월일·비자 프로필·분석 결과가 없는 사용자). 여유자금이 None이면
    '요청 월 납입액 > 여유자금' 판정도 하지 않는다.
    """
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
    visa = profile.get("visa")
    result["visa"] = (
        UNKNOWN if visas is None or visa is None else (OK if visa in visas else VIOLATION)
    )

    age_min, age_max = value(product, "ageMin"), value(product, "ageMax")
    age = profile.get("age")
    if (age_min is None and age_max is None) or age is None:
        result["age"] = UNKNOWN
    else:
        too_young = age_min is not None and age < age_min
        too_old = age_max is not None and age > age_max
        result["age"] = VIOLATION if too_young or too_old else OK

    term = shortest_term(product)
    stay = remaining_stay_months(profile, as_of)
    result["stayTerm"] = (
        UNKNOWN if term is None or stay is None else (OK if term <= stay else VIOLATION)
    )

    # 여유자금은 월 흐름이라 적금의 월 최소액과만 비교한다. 예금(목돈)은 프로필에 목돈 값이
    # 없으므로 최소 가입액 조건을 적용하지 않는다(위반도 판정 불가도 아님).
    spare = profile.get("spareKrw")
    if kind == "SAVINGS":
        minimum = value(product, "monthlyMinKrw")
        result["spare"] = (
            UNKNOWN if minimum is None or spare is None else (OK if minimum <= spare else VIOLATION)
        )

    if "maxTermMonths" in conditions:
        result["requestTerm"] = (
            UNKNOWN if term is None else (OK if term <= conditions["maxTermMonths"] else VIOLATION)
        )

    if "monthlyKrw" in conditions:
        amount = conditions["monthlyKrw"]
        low = value(product, "monthlyMinKrw")
        high = _upper(value(product, "monthlyMaxKrw"), no_limit(product, "monthlyMaxKrw"), amount)
        if spare is not None and amount > spare:
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


# ②③: 조건은 챗봇 도구 인자와 같은 형태로 받는다.
# 오프라인 평가에서는 질문 세트의 요청 조건을 이 형태로 바꿔 넣는다.
REASON_SURPLUS = "REQUEST_EXCEEDS_SURPLUS"
REASON_STAY = "STAY_TOO_SHORT"
REASON_NONE = "NO_MATCHING_PRODUCT"


def request_from_args(args: Mapping[str, Any]) -> dict[str, Any]:
    """도구 인자 → check_product가 읽는 요청 조건."""
    pairs = [
        ("kind", args.get("productType")),
        ("maxTermMonths", args.get("maxTermMonths")),
        ("monthlyKrw", args.get("monthlyAmountKrw")),
        ("savingType", args.get("accrual")),
    ]
    return {"conditions": {name: v for name, v in pairs if v is not None}}


FOREIGNER_EXPLICIT = "requireForeignerExplicit"


def failed_condition(
    product: Product,
    profile: Profile,
    args: Mapping[str, Any],
    as_of: date,
    ignore: frozenset[str] = frozenset(),
) -> str | None:
    """상품을 빼는 첫 조건 이름(CONDITIONS 순서, 마지막이 외국인 명시 요청). 통과하면 None.

    위반, 요청 조건 판정 불가, (요청 시) 외국인 명시 아님이 상품을 뺀다. ignore 조건은 보지 않는다.
    화면 추천 API(#84)는 이 이름으로 제외 상품을 센다.
    """
    checks = check_product(product, profile, request_from_args(args), as_of)
    for name in CONDITIONS:
        verdict = None if name in ignore else checks.get(name)
        if verdict == VIOLATION or (verdict == UNKNOWN and name in REQUEST_CONDITIONS):
            return name
    if args.get(FOREIGNER_EXPLICIT) and value(product, "foreignerEligible") != "explicit":
        return FOREIGNER_EXPLICIT
    return None


def passes(
    product: Product,
    profile: Profile,
    args: Mapping[str, Any],
    as_of: date,
    ignore: frozenset[str] = frozenset(),
) -> bool:
    """위반 없음 + 요청 조건 판정 불가 없음 + (요청 시) 외국인 명시. ignore 조건은 보지 않는다."""
    return failed_condition(product, profile, args, as_of, ignore) is None


def abstain_reason(
    pool: Sequence[str],
    products: Mapping[str, Product],
    profile: Profile,
    args: Mapping[str, Any],
    as_of: date,
) -> str:
    """남는 상품이 없을 때의 이유 코드(9-1 순서). 프로필 값이 없으면 그 이유는 내지 않는다."""
    amount = args.get("monthlyAmountKrw")
    spare = profile.get("spareKrw")
    if amount is not None and spare is not None and amount > spare:
        return REASON_SURPLUS
    without_stay = frozenset({"stayTerm"})
    stay_known = profile.get("stayExpiry") is not None
    if stay_known and any(
        passes(products[pid], profile, args, as_of, without_stay) for pid in pool
    ):
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

"""챗봇 금융상품 추천 도구 ``recommend_savings_products``(이슈 #65).

- LLM은 질문에 나온 조건만 인자로 넘긴다. 사용자 id·프로필(체류 만료·나이·비자·여유자금)은 요청마다
  고정되어 LLM 인자로 바꿀 수 없다.
- 조건 판정과 기권 이유는 코드(app/products/recommender.py ③)가 정한다. 후보는 조건표 전체다.
- 순서: 상품설명서 검색(queryKo, app/products/search.py)이 있으면 B(검색 점수), 없거나 실패하면
  A(조건표 순서). 둘 다 foreignerEligible=explicit 상품을 먼저 둔다.
- LLM에는 답에 쓸 값만 준다: 상품 이름·종류·기간·금액 범위·적립 방식·외국인 가입 근거, 근거 인용,
  판정하지 못한 조건, 기권 이유. 금리는 조건표에 없어서 주지 않는다. 문서 id는 주지 않는다.
- 프로필 계약: specs/chat/product_profile_contract_v1.md. 여유자금은 INT-06(latestAnalysis.spare).
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.chat.tools import UNAVAILABLE, ToolOutcome
from app.integrations.spring_client import SpringApiError, SpringInternalClient
from app.products import recommender as rec
from app.products.search import search_order

logger = logging.getLogger(__name__)

RECOMMEND_SAVINGS_PRODUCTS = "recommend_savings_products"
_AI_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = _AI_ROOT / "specs" / "rag" / "product_catalog_v1.json"
# 테스트·fake 모드·Spring 미설정·측정에서 쓰는 프로필(데모 값, 실제 사용자 데이터가 아님).
PROFILE_FIXTURE = _AI_ROOT / "tests" / "fixtures" / "chat" / "product_profiles.json"
MAX_MONTHLY_AMOUNT = 100_000_000
# 도구 인자(영어) ↔ 조건표 값(한국어).
ACCRUAL_TO_CATALOG = {"FLEXIBLE": "자유", "FIXED": "정액"}
ACCRUAL_FROM_CATALOG = {v: k for k, v in ACCRUAL_TO_CATALOG.items()}
# 사용자에게 판정 불가로 안내할 수 있는 프로필 조건(체크 이름 → 도구 결과에 쓰는 이름).
_PROFILE_CHECKS = {"visa": "visa", "age": "age", "stayTerm": "remainingStay", "spare": "spareMoney"}

TOOL_SCHEMA: dict[str, Any] = {
    "name": RECOMMEND_SAVINGS_PRODUCTS,
    "description": (
        "Recommend Shinhan installment savings (적금) and time deposit (정기예금) products for the "
        "current user. Code filters the product catalog by the user's remaining stay, monthly "
        "spare money and age, and by the conditions you pass. Pass only conditions the user "
        "stated; leave the others out. Returns up to 5 products in order with their terms, "
        "evidence quotes from the product description and the conditions that could not be "
        "checked (for example visa or age), or, when nothing fits, abstainReason: "
        "REQUEST_EXCEEDS_SURPLUS (the monthly amount is more than the user's monthly spare "
        "money), STAY_TOO_SHORT (the fitting products last longer than the user's remaining "
        "stay) or NO_MATCHING_PRODUCT. Has no interest rates. Not for checking accounts, cards "
        "or loans."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "productType": {
                "type": "string",
                "enum": ["SAVINGS", "DEPOSIT"],
                "description": (
                    "SAVINGS: installment savings, paying in every month (also 'savings "
                    "account'). DEPOSIT: time deposit of a lump sum (also 'fixed deposit')."
                ),
            },
            "maxTermMonths": {
                "type": "integer",
                "minimum": 1,
                "maximum": 120,
                "description": "Longest contract term the user accepts, in months.",
            },
            "monthlyAmountKrw": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_MONTHLY_AMOUNT,
                "description": "Amount the user wants to pay in each month, KRW.",
            },
            "accrual": {
                "type": "string",
                "enum": ["FLEXIBLE", "FIXED"],
                "description": (
                    "FLEXIBLE: pay in any amount any time. FIXED: the same amount every month."
                ),
            },
            "requireForeignerExplicit": {
                "type": "boolean",
                "description": (
                    "true only when the user asks for products made for or clearly open to "
                    "foreigners."
                ),
            },
            "queryKo": {
                "type": "string",
                "maxLength": 100,
                "description": (
                    "Short Korean search phrase for the request, used only to order the "
                    "products, e.g. '3개월 이하 단기 적금'."
                ),
            },
        },
        "additionalProperties": False,
    },
}


# v4.1(2026-11-17, v4 1차 측정 결과를 보고 수정): 인자 오류 두 유형을 줄이는 설명.
# v4 측정을 그대로 다시 낼 수 있도록 v4 스키마는 바꾸지 않고 프롬프트 버전별로 고른다.
TOOL_SCHEMA_V4_1: dict[str, Any] = copy.deepcopy(TOOL_SCHEMA)
TOOL_SCHEMA_V4_1["input_schema"]["properties"]["productType"]["description"] = (
    "SAVINGS: installment savings, paying in every month. Whenever the user mentions savings "
    "(a savings account, savings plan or savings product), pass SAVINGS. DEPOSIT: time deposit "
    "of a lump sum (also 'fixed deposit')."
)
TOOL_SCHEMA_V4_1["input_schema"]["properties"]["accrual"]["description"] = (
    "FLEXIBLE: pay in any amount any time. FIXED: only when the user says they will pay the same "
    "amount each time. A monthly amount or 'every month' alone is not FIXED; leave accrual out."
)
TOOL_SCHEMAS_BY_PROMPT: dict[str, dict[str, Any]] = {
    "v4": TOOL_SCHEMA,
    "v4.1": TOOL_SCHEMA_V4_1,
    "v4.2": TOOL_SCHEMA_V4_1,
    "v4.3a": TOOL_SCHEMA_V4_1,
    # v4.4 = v4.2 + 환율 진단 규칙이라 상품 도구 설명도 v4.1용 그대로다.
    "v4.4": TOOL_SCHEMA_V4_1,
    # v4.5 = v4.4 + 목표·출처·카드 규칙. 상품 도구 설명은 그대로다.
    "v4.5": TOOL_SCHEMA_V4_1,
    # v4.4d = v4.4 + 답변 표시 규칙(#116), v4.6 = v4.5 + 같은 규칙. 상품 도구 설명은 그대로다.
    "v4.4d": TOOL_SCHEMA_V4_1,
    "v4.6": TOOL_SCHEMA_V4_1,
}


class RecommendConditions(BaseModel):
    """추천 조건(채팅 도구 인자와 화면 추천 API의 conditions가 같은 이름·범위를 쓴다)."""

    model_config = ConfigDict(extra="forbid")

    productType: Literal["SAVINGS", "DEPOSIT"] | None = None  # noqa: N815 - LLM 인자 이름
    maxTermMonths: int | None = Field(default=None, ge=1, le=120)  # noqa: N815
    monthlyAmountKrw: int | None = Field(default=None, ge=1, le=MAX_MONTHLY_AMOUNT)  # noqa: N815
    accrual: Literal["FLEXIBLE", "FIXED"] | None = None
    requireForeignerExplicit: bool = False  # noqa: N815

    def conditions(self) -> dict[str, Any]:
        """채점·결과에 쓰는 조건(검색어 제외, 넘긴 것만)."""
        data = self.model_dump(exclude={"queryKo"}, exclude_none=True)
        if not data.get("requireForeignerExplicit"):
            data.pop("requireForeignerExplicit", None)
        return data

    def recommender_args(self) -> dict[str, Any]:
        return {
            "productType": self.productType,
            "maxTermMonths": self.maxTermMonths,
            "monthlyAmountKrw": self.monthlyAmountKrw,
            "accrual": ACCRUAL_TO_CATALOG.get(self.accrual or ""),
            "requireForeignerExplicit": self.requireForeignerExplicit,
        }


class RecommendInput(RecommendConditions):
    """recommend_savings_products 인자(LLM이 채움): 조건 + 순서 B 검색어."""

    queryKo: str | None = Field(default=None, max_length=100)  # noqa: N815


class ProductProfile(BaseModel):
    """product-profile 계약 응답(camelCase 그대로). 모르는 필드는 무시한다."""

    model_config = ConfigDict(extra="ignore")

    visaTypeCode: str | None = Field(default=None, max_length=20)  # noqa: N815
    stayExpiresOn: date | None = None  # noqa: N815
    age: int | None = Field(default=None, ge=0, le=150)
    asOf: date  # noqa: N815


@dataclass(frozen=True)
class ProductContext:
    """추천기가 읽는 프로필과 기준일. spare_krw가 None이면 분석 결과가 없다."""

    profile: ProductProfile
    spare_krw: int | None

    def recommender_profile(self) -> dict[str, Any]:
        return {
            "visa": self.profile.visaTypeCode,
            "stayExpiry": None
            if self.profile.stayExpiresOn is None
            else self.profile.stayExpiresOn.isoformat(),
            "age": self.profile.age,
            "spareKrw": self.spare_krw,
        }


class ProductContextSource(Protocol):
    async def fetch(self, user_id: int, request_id: str | None) -> ProductContext: ...


class MeasuredProfile(ProductProfile):
    """측정 전용 헤더로 받는 프로필 값(질문 파일의 profiles). spareKrw는 INT-06 여유자금 대신."""

    model_config = ConfigDict(extra="forbid")

    spareKrw: int | None = Field(default=None, ge=0)  # noqa: N815


class FixtureProductContext:
    """fake 모드이거나 Spring 설정이 없을 때의 프로필 하나.

    profile이 없으면 fixture 기본(P1), fixture id(P1~P5)면 그 프로필, JSON 객체면 그 값
    (측정 전용: 질문 파일의 profiles를 그대로 싣는다. hold-out의 새 프로필도 쓸 수 있다).
    """

    def __init__(self, profile: str | None = None, path: Path = PROFILE_FIXTURE) -> None:
        if profile and profile.lstrip().startswith("{"):
            try:
                measured = MeasuredProfile.model_validate_json(profile)
            except ValidationError as exc:
                fields = sorted({str(e["loc"][0]) for e in exc.errors() if e["loc"]})
                raise ValueError(f"invalid product profile: {', '.join(fields) or 'body'}") from exc
            base = ProductProfile.model_validate(measured.model_dump(exclude={"spareKrw"}))
            self._context = ProductContext(base, measured.spareKrw)
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        key = profile or data["default"]
        if key not in data["profiles"]:
            raise ValueError(f"unknown fixture profile: {key}")
        raw = data["profiles"][key]
        self._context = ProductContext(ProductProfile.model_validate(raw), raw.get("spareKrw"))

    async def fetch(self, user_id: int, request_id: str | None) -> ProductContext:
        return self._context


def _money(value: Any) -> int | None:
    return int(value["amount"]) if isinstance(value, Mapping) and "amount" in value else None


def spare_from_finance(context: Mapping[str, Any]) -> int | None:
    """INT-06 응답의 월 여유자금. 분석 결과가 없으면 None.

    balance(hanstep-backend #151·#153, 직접 입력 우선)가 있으면 balance.spare를 쓴다(PRD-02와 같은
    값, #114). 없으면 latestAnalysis.spare(기존 동작). 프롬프트 버전과 상관없다.
    """
    balance = context.get("balance")
    if isinstance(balance, Mapping) and (spare := _money(balance.get("spare"))) is not None:
        return spare
    analysis = context.get("latestAnalysis")
    if not isinstance(analysis, Mapping):
        return None
    return _money(analysis.get("spare"))


class SpringProductContext:
    """anthropic 모드이고 Spring이 설정되어 있을 때: product-profile과 INT-06을 함께 부른다."""

    def __init__(self, client: SpringInternalClient, timeout: float) -> None:
        self._client = client
        self._timeout = timeout

    async def fetch(self, user_id: int, request_id: str | None) -> ProductContext:
        profile, finance = await asyncio.gather(
            asyncio.to_thread(
                self._client.product_profile,
                user_id,
                timeout=self._timeout,
                request_id=request_id,
            ),
            asyncio.to_thread(
                self._client.finance_context,
                user_id,
                timeout=self._timeout,
                request_id=request_id,
            ),
        )
        return ProductContext(ProductProfile.model_validate(profile), spare_from_finance(finance))


class ProductRanker(Protocol):
    def rank(self, query: str) -> list[str]: ...


def load_catalog(path: Path = CATALOG_PATH) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))["products"]
    return products


def _range(product: Mapping[str, Any], low: str, high: str) -> dict[str, Any] | None:
    lo, hi = rec.value(product, low), rec.value(product, high)
    if lo is None and hi is None and not rec.no_limit(product, high):
        return None
    out: dict[str, Any] = {"min": lo, "max": hi}
    if rec.no_limit(product, high):
        out["max"] = "no limit"
    return out


def _evidence(product: Mapping[str, Any]) -> list[dict[str, Any]]:
    """조건표 근거 인용(같은 문장은 한 번). 판정에 쓰는 항목만, 최대 4개."""
    fields = (
        ("term", ("termOptions", "termMonthsMin")),
        ("amount", ("monthlyMinKrw", "monthlyMaxKrw", "depositMinKrw")),
        ("accrual", ("savingType",)),
        ("foreigners", ("foreignerEligible",)),
    )
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for about, names in fields:
        for name in names:
            entry = product.get(name)
            source = entry.get("source") if isinstance(entry, Mapping) else None
            if not source or not source.get("quote") or source["quote"] in seen:
                continue
            seen.add(source["quote"])
            out.append({"about": about, "quote": source["quote"], "page": source.get("page")})
            break
    return out[:4]


def product_view(product: Mapping[str, Any], checks: Mapping[str, str]) -> dict[str, Any]:
    """LLM에 주는 상품 하나(답에 쓸 값만, 문서 id 없음)."""
    kind = rec.value(product, "kind")
    options = rec.value(product, "termOptions")
    term: Any = list(options) if options else _range(product, "termMonthsMin", "termMonthsMax")
    view: dict[str, Any] = {
        "name": rec.value(product, "nameKo"),
        "type": kind,
        "termMonths": term,
    }
    if kind == "SAVINGS":
        view["monthlyAmountKrw"] = _range(product, "monthlyMinKrw", "monthlyMaxKrw")
        view["accrual"] = ACCRUAL_FROM_CATALOG.get(rec.value(product, "savingType") or "")
    else:
        view["depositAmountKrw"] = _range(product, "depositMinKrw", "depositMaxKrw")
    view["foreigners"] = rec.value(product, "foreignerEligible")
    view["notChecked"] = [
        label for check, label in _PROFILE_CHECKS.items() if checks.get(check) == rec.UNKNOWN
    ]
    view["evidence"] = _evidence(product)
    return view


def _quoted(product: Mapping[str, Any], field: str) -> tuple[Any, Mapping[str, Any]] | None:
    """조건표 값과 원문 근거. 값이나 근거가 없으면 None(지어내지 않는다)."""
    entry = product.get(field)
    if not isinstance(entry, Mapping) or entry.get("value") is None or not entry.get("source"):
        return None
    return entry, entry["source"]


def rate_fields(product: Mapping[str, Any]) -> dict[str, Any]:
    """baseRate·maxRate·feature: 조건표 원문 값을 그대로 옮긴다."""
    out: dict[str, Any] = {"baseRate": None, "maxRate": None, "feature": None}
    if base := _quoted(product, "baseRate"):
        entry, source = base
        out["baseRate"] = {
            "ratePct": entry["value"],
            "asOf": entry.get("asOf"),
            "quote": source["quote"],
            "page": source["page"],
        }
    if top := _quoted(product, "maxRate"):
        entry, source = top
        out["maxRate"] = {
            "ratePct": entry["value"],
            "quote": source["quote"],
            "page": source["page"],
        }
    if feature := _quoted(product, "feature"):
        _, source = feature
        out["feature"] = {"quote": source["quote"], "page": source["page"]}
    return out


def screen_item(product: Mapping[str, Any], checks: Mapping[str, str]) -> dict[str, Any]:
    """PRD-02 products[] 항목: product_view 값에 상품 id·체류 판정·금리·특징을 더한다(키는 늘 같음).

    화면 추천 API(app/products/screen.py)와 채팅 카드(PRODUCT_RECOMMEND_CARD)가 함께 쓴다.
    """
    view = product_view(product, checks)
    stay = checks.get("stayTerm")
    return {
        "productId": product["productId"],
        "nameKo": view["name"],
        "kind": view["type"],
        "termMonths": view["termMonths"],
        "monthlyAmountKrw": view.get("monthlyAmountKrw"),
        "depositAmountKrw": view.get("depositAmountKrw"),
        "accrual": view.get("accrual"),
        "foreignerEligible": view["foreigners"],
        "notChecked": view["notChecked"],
        "stayFits": True if stay == rec.OK else None,
        **rate_fields(product),
        "evidence": view["evidence"],
    }


def excluded_summary(
    excluded: Sequence[str],
    products: Mapping[str, Mapping[str, Any]],
    profile: Mapping[str, Any],
    args: Mapping[str, Any],
    as_of: date,
) -> list[dict[str, Any]]:
    """제외 상품을 처음 걸린 조건 하나로 세어 판정 순서대로 준다."""
    counts: dict[str, int] = {}
    for pid in excluded:
        reason = rec.failed_condition(products[pid], profile, args, as_of)
        if reason is None:  # 호출하는 쪽이 조건에 걸린 상품만 넘긴다
            raise RuntimeError(f"excluded product passes: {pid}")
        counts[reason] = counts.get(reason, 0) + 1
    order = (*rec.CONDITIONS, rec.FOREIGNER_EXPLICIT)
    return [{"reason": name, "count": counts[name]} for name in order if name in counts]


# 금융상품 추천 카드(chat.done blocks, specs/chat/product_recommend_card_v1.md, #114).
PRODUCT_RECOMMEND_CARD = "PRODUCT_RECOMMEND_CARD"


class ProductRecommendCardBlock(BaseModel):
    """카드 형식 확인용. 값은 추천기 결과에서만 채운다(LLM이 쓴 숫자 없음)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["PRODUCT_RECOMMEND_CARD"] = "PRODUCT_RECOMMEND_CARD"
    asOf: str  # noqa: N815 - 계약 이름(PRD-02)
    remainingStayMonths: int | None  # noqa: N815
    abstainReason: str | None  # noqa: N815
    products: list[dict[str, Any]] = Field(max_length=rec.TOP_K)
    excludedSummary: list[dict[str, Any]]  # noqa: N815


def merge_product_card(blocks: list[dict[str, Any]], card: dict[str, Any]) -> None:
    """한 턴에 카드 1장: 앞선 상품 카드를 지우고 이번(마지막 성공) 카드를 이 호출 자리에 둔다."""
    blocks[:] = [b for b in blocks if b.get("type") != PRODUCT_RECOMMEND_CARD]
    blocks.append(card)


def _source(product: Mapping[str, Any]) -> dict[str, Any]:
    """agent.sources 항목(계약 3절 제안): 문서 id는 chunkRef, 쪽은 상품명 근거의 쪽."""
    name = product.get("nameKo")
    page = (name.get("source") or {}).get("page") if isinstance(name, Mapping) else None
    return {
        "type": "PRODUCT_DOCUMENT",
        "id": None,
        "chunkRef": product["productId"],
        "pageLabel": None if page is None else f"p.{page}",
    }


class ProductTool:
    """요청 하나에 묶인다: 사용자 id·요청 id·프로필 출처는 LLM이 바꿀 수 없다."""

    def __init__(
        self,
        source: ProductContextSource,
        *,
        user_id: int,
        request_id: str | None,
        timeout: float,
        ranker: ProductRanker | None = None,
        catalog: Sequence[Mapping[str, Any]] | None = None,
        schema: dict[str, Any] = TOOL_SCHEMA,
        card: bool = False,
    ) -> None:
        self._source = source
        self._schema = schema
        # 금융상품 추천 카드(PRODUCT_RECOMMEND_CARD). 이 카드를 설명하는 프롬프트(v4.5)에서만 켠다.
        self._card = card
        self._user_id = user_id
        self._request_id = request_id
        self._timeout = timeout
        self._ranker = ranker
        self._catalog = list(catalog if catalog is not None else load_catalog())

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [self._schema]

    async def run(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        started = time.perf_counter()

        def outcome(status: str, content: Any, **extra: Any) -> ToolOutcome:
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            latency = round((time.perf_counter() - started) * 1000)
            return ToolOutcome(name, arguments, status, text, latency, **extra)

        if name != RECOMMEND_SAVINGS_PRODUCTS:
            return outcome("REJECTED", "unknown tool", error_code="UNKNOWN_TOOL")
        try:
            parsed = RecommendInput.model_validate(arguments)
        except ValidationError as exc:
            fields = sorted({str(e["loc"][0]) for e in exc.errors() if e["loc"]})
            return outcome(
                "REJECTED",
                f"invalid arguments: {', '.join(fields) or 'body'}",
                error_code="INVALID_ARGUMENTS",
                measurement={"arguments": arguments},
            )
        try:
            context = await asyncio.wait_for(
                self._source.fetch(self._user_id, self._request_id), self._timeout
            )
        except (TimeoutError, httpx.TimeoutException):
            return outcome("TIMEOUT", UNAVAILABLE, error_code="SPRING_TIMEOUT")
        except SpringApiError as exc:
            return outcome("FAILED", UNAVAILABLE, error_code=f"SPRING_HTTP_{exc.status}")
        except ValidationError:
            return outcome("FAILED", UNAVAILABLE, error_code="INVALID_RESPONSE")
        except Exception as exc:  # transport and anything else: answer without the data
            logger.warning("product context failed: %s", type(exc).__name__)
            return outcome("FAILED", UNAVAILABLE, error_code="SPRING_ERROR")

        catalog_order = [p["productId"] for p in self._catalog]
        pool, ranked_by = await self._order(parsed.queryKo, catalog_order)
        products = {p["productId"]: p for p in self._catalog}
        profile = context.recommender_profile()
        as_of = context.profile.asOf
        args = parsed.recommender_args()
        result = rec.recommend_filtered(
            pool, products, profile, args, as_of, explicit_first=True, with_reason=True
        )
        request = rec.request_from_args(args)
        views = [
            product_view(products[pid], rec.check_product(products[pid], profile, request, as_of))
            for pid in result["recommended"]
        ]
        notes = []
        if context.spare_krw is None:
            notes.append(
                "No cash-flow analysis yet: monthly spare money was not checked. "
                "Connecting MyData lets the app check it."
            )
        if context.profile.stayExpiresOn is None:
            notes.append("Stay expiry date unknown: contract terms were not checked against it.")
        content = {
            "asOf": as_of.isoformat(),
            "user": {
                "remainingStayMonths": rec.remaining_stay_months(profile, as_of),
                "monthlySpareKrw": context.spare_krw,
            },
            "conditions": parsed.conditions(),
            "products": views,
            "abstainReason": result["reason"],
            "notes": notes,
        }
        measurement = {
            "arguments": arguments,
            "conditions": parsed.conditions(),
            "recommended": list(result["recommended"]),
            "abstainReason": result["reason"],
            "rankedBy": ranked_by,
            "content": content,
        }
        blocks = []
        if self._card:
            card = self._card_block(pool, products, profile, args, as_of, result, request)
            if card is not None:
                blocks.append(card)
        return outcome(
            "SUCCEEDED",
            content,
            sources=[_source(products[pid]) for pid in result["recommended"]],
            blocks=blocks,
            measurement=measurement,
        )

    @staticmethod
    def _card_block(
        pool: Sequence[str],
        products: Mapping[str, Mapping[str, Any]],
        profile: Mapping[str, Any],
        args: Mapping[str, Any],
        as_of: date,
        result: Mapping[str, Any],
        request: Any,
    ) -> dict[str, Any] | None:
        """LLM이 받은 상품을 같은 순서로(최대 5개) PRD-02 이름으로 담은 카드. 기권이면 products=[].

        카드 값만 형식이 어긋나면 카드를 내지 않는다(답변은 그대로).
        """
        try:
            excluded = [
                pid for pid in pool if rec.failed_condition(products[pid], profile, args, as_of)
            ]
            card = ProductRecommendCardBlock(
                asOf=as_of.isoformat(),
                remainingStayMonths=rec.remaining_stay_months(profile, as_of),
                abstainReason=result["reason"],
                products=[
                    screen_item(
                        products[pid], rec.check_product(products[pid], profile, request, as_of)
                    )
                    for pid in result["recommended"]
                ],
                excludedSummary=excluded_summary(excluded, products, profile, args, as_of),
            )
        except (ValidationError, KeyError, TypeError, RuntimeError) as exc:
            logger.warning("product card skipped: %s", type(exc).__name__)
            return None
        return card.model_dump(mode="json")

    async def _order(self, query: str | None, catalog_order: list[str]) -> tuple[list[str], str]:
        return await order_pool(self._ranker, query, catalog_order, self._timeout)


async def order_pool(
    ranker: ProductRanker | None, query: str | None, catalog_order: list[str], search_seconds: float
) -> tuple[list[str], str]:
    """순서 B(검색) 또는 A(조건표 순서)와 그 이름. 채팅 도구와 화면 추천 API(#84)가 함께 쓴다."""
    if ranker is None:
        return catalog_order, "catalog"
    if not query or not query.strip():
        return catalog_order, "catalog_no_query"
    try:
        ranked = await asyncio.wait_for(
            asyncio.to_thread(ranker.rank, query.strip()), search_seconds
        )
    except Exception as exc:  # search is only for order: fall back to the catalog order
        logger.warning("product search failed: %s", type(exc).__name__)
        return catalog_order, "catalog_search_failed"
    return search_order(ranked, catalog_order), "search"

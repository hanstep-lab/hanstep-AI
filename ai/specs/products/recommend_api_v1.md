# 금융상품 추천 화면 ↔ AI 내부 API 계약 v1 (products/recommend)

FP-001 금융상품 추천 화면이 쓰는 AI 내부 API의 요청·응답 형식이다(이슈 #84). AI 쪽에서 정한 계약이며,
BE(Spring)가 이 API를 불러 FE 응답(`GET /api/v1/finance/products/recommend`) 모양으로 조립한다.
구현하면서 바꿀 곳이 생기면 이 문서를 먼저 고친다.

- 채팅 상품 추천 도구(`recommend_savings_products`, `app/chat/product_tools.py`)와 **같은 추천기**를 쓴다.
  조건 판정·기권 이유는 `app/products/recommender.py`의 `recommend_filtered`, 상품 항목은 `product_view`다.
  같은 프로필·조건·검색어면 채팅 상위 5개 = 이 API `products`의 앞 5개다.
- LLM을 부르지 않는다. 금리·특징은 조건표(`specs/rag/product_catalog_v1.json`)에 넣은 상품설명서 원문 값만 옮긴다.
- 인증: 다른 Spring→AI 내부 API와 같이 `X-Internal-Token` 헤더, 요청 추적용 `X-Request-Id`.
- 프로필은 Spring이 넘긴다(AI가 Spring을 다시 부르지 않는다). 저장 목표 기준은 없다.

## 1. 요청

`POST /internal/v1/products/recommend`

```json
{
  "asOf": "2026-11-17",
  "profile": {"visaTypeCode": "E-9", "age": 30, "stayExpiresOn": "2027-10-04"},
  "monthlySpareKrw": 675317,
  "conditions": {"productType": "SAVINGS", "maxTermMonths": 12},
  "queryKo": null
}
```

| 필드 | 형식 | 필수 | 뜻 |
|---|---|---|---|
| asOf | date, ISO `YYYY-MM-DD` | 예 | 판정 기준일. product-profile 응답의 `asOf`(Asia/Seoul)를 그대로 넘긴다 |
| profile | object | 예 | 아래 세 필드. 이름·`null` 규칙은 `specs/chat/product_profile_contract_v1.md` 1절과 같다 |
| profile.visaTypeCode | string(최대 20자) \| null | 아니오(기본 null) | 비자 종류 코드. 없으면 비자 조건 판정 불가 |
| profile.age | int(0~150) \| null | 아니오(기본 null) | `asOf` 기준 만 나이. 없으면 나이 조건 판정 불가 |
| profile.stayExpiresOn | date \| null | 아니오(기본 null) | 체류 만료일. 없으면 체류 기간 조건 판정 불가, `STAY_TOO_SHORT` 안 냄 |
| monthlySpareKrw | int(0 이상) \| null | 아니오(기본 null) | 월 여유자금(INT-06 `latestAnalysis.spare.amount`). 분석 결과가 없으면 null |
| conditions | object \| null | 아니오 | 화면에서 고른 조건. 없거나 `{}`면 조건 없음. 아래 표 |
| queryKo | string(최대 100자) \| null | 아니오 | 순서 B(상품설명서 검색)에 쓰는 한국어 검색어. 없으면 순서 A |

`conditions`(채팅 도구 인자와 같은 이름·범위, 넘긴 것만 적용):

| 필드 | 형식 | 뜻 |
|---|---|---|
| productType | `SAVINGS` \| `DEPOSIT` | 적금 / 정기예금 |
| maxTermMonths | int 1~120 | 받아들이는 가장 긴 계약 기간(개월) |
| monthlyAmountKrw | int 1~100,000,000 | 매달 넣을 금액(원) |
| accrual | `FLEXIBLE` \| `FIXED` | 자유적립 / 정액적립 |
| requireForeignerExplicit | bool(기본 false) | true면 상품설명서에 외국인 가입이 명시된 상품만 |

- 모르는 필드가 있으면(요청 본문·`profile`·`conditions` 모두) 400이다.

## 2. 응답 200

```json
{
  "asOf": "2026-11-17",
  "remainingStayMonths": 10,
  "rankedBy": "catalog_no_query",
  "abstainReason": null,
  "products": [
    {
      "productId": "doc-a32e7bfdf57e",
      "nameKo": "슈퍼SOL 웰컴 정기예금",
      "kind": "DEPOSIT",
      "termMonths": [6, 12],
      "monthlyAmountKrw": null,
      "depositAmountKrw": {"min": 500000, "max": 20000000},
      "accrual": null,
      "foreignerEligible": "inferred",
      "notChecked": ["visa"],
      "stayFits": true,
      "baseRate": {
        "ratePct": 3.3,
        "asOf": "2026-11-09",
        "quote": "기본이자율 (2026.11.09 현재, 세전) 6개월 : 연 3.3% 12개월 : 연 3.6%",
        "page": 2
      },
      "maxRate": null,
      "feature": {"quote": "신한 슈퍼 SOL 신규 고객 및 급여 첫 거래 고객에게 우대금리를 제공하는 정기예금", "page": 1},
      "evidence": [
        {"about": "term", "quote": "계약기간 6개월, 12개월", "page": 1},
        {"about": "amount", "quote": "50만원 이상 2,000만원 이하", "page": 1},
        {"about": "foreigners", "quote": "실명의 개인 및 개인사업자", "page": 1}
      ]
    }
  ],
  "excludedSummary": [
    {"reason": "recommendable", "count": 1},
    {"reason": "stayTerm", "count": 5}
  ]
}
```

(위 응답은 Reyes 프로필(E-9, 만료 2027-10-04, 여유자금 675,317원, 조건 없음)의 실제 결과에서 상품 하나만 남긴 것이다.
이 경우 `products`는 7개다.)

| 필드 | 형식 | 뜻 |
|---|---|---|
| asOf | date | 요청의 `asOf` |
| remainingStayMonths | int \| null | `asOf`부터 체류 만료일까지 채운 개월 수(추천기 `remaining_stay_months`). 만료일이 없으면 null |
| rankedBy | string | 순서 출처. `search`(B) / `catalog`(검색 기능 없음, A) / `catalog_no_query`(검색어 없음, A) / `catalog_search_failed`(검색 실패, A) |
| abstainReason | string \| null | `products`가 비었을 때만: `REQUEST_EXCEEDS_SURPLUS` / `STAY_TOO_SHORT` / `NO_MATCHING_PRODUCT`. 아니면 null |
| products | array | 조건을 통과한 상품 **전부**, 아래 순서 |
| excludedSummary | array | 제외된 상품의 이유별 개수. 제외 상품이 없으면 `[]` |

### 2-1. 순서

채팅 도구와 같다(`specs/rag/product_recommend_v1_results.md` 12절).

- `queryKo`가 있고 상품설명서 검색이 준비돼 있으면 **B**(검색 점수 순, 검색에 안 걸린 상품은 조건표 순서로 뒤에).
  검색어가 없거나 검색이 불가·실패하면 **A**(조건표 순서).
- 둘 다 `foreignerEligible=explicit` 상품을 먼저 둔다(안에서는 위 순서 유지).

### 2-2. products[] 항목

기간·금액·적립 방식·외국인 가입·판정 불가 조건·근거는 채팅 `product_view`와 같은 값·이름이다.
화면에서 키가 늘 같도록 해당 없는 값도 키를 두고 null로 보낸다.

| 필드 | 형식 | 뜻 |
|---|---|---|
| productId | string | 조건표 `productId`(상품설명서 문서 id, 예: `doc-7b32d0e385aa`) |
| nameKo | string | 상품명 |
| kind | `SAVINGS` \| `DEPOSIT` | 적금 / 정기예금 |
| termMonths | int[] \| {min, max} \| null | 정해진 기간 중 고르는 상품은 배열, 범위형은 `{min, max}` |
| monthlyAmountKrw | {min, max} \| null | 적금의 월 납입 범위(원). `max`가 `"no limit"`이면 원문이 '제한없음'. 예금은 null |
| depositAmountKrw | {min, max} \| null | 예금의 가입 금액 범위(원). 적금은 null |
| accrual | `FLEXIBLE` \| `FIXED` \| null | 적금의 적립 방식. 예금은 null |
| foreignerEligible | `explicit` \| `inferred` \| null | 외국인 가입: 원문 명시 / 해석 / 근거 없음 |
| notChecked | string[] | 프로필 값이 없거나 조건표 근거가 없어 판정하지 못한 조건: `visa` / `age` / `remainingStay` / `spareMoney` |
| stayFits | true \| null | 가장 짧은 계약 기간 ≤ 남은 체류 개월이면 true, 판정 불가면 null(넘는 상품은 제외되어 여기 없음) |
| baseRate | object \| null | 원문의 기본금리. 아래 |
| maxRate | object \| null | 원문에 **명시된** 최고(최종) 금리만. 아래 |
| feature | {quote, page} \| null | 원문 '상품특징' 문장 인용과 쪽 |
| evidence | {about, quote, page}[] | 판정에 쓴 조건표 근거 인용(최대 4개, `about`: term·amount·accrual·foreigners) |

`baseRate`:

| 필드 | 형식 | 뜻 |
|---|---|---|
| ratePct | number | 연 이율(%) 원문 값. 기간별로 다르면 **가장 짧은 가입 기간**의 값 |
| asOf | date \| null | 원문의 금리 기준일. 원문에 없으면 null |
| quote | string | 금리 원문 인용(기간별이면 나머지 기간 값도 인용에 둔다) |
| page | int | 인용 쪽 |

`maxRate`: `{ratePct, quote, page}`. 원문이 '최고이자율'·'최종이자율' 항목에 값을 적은 경우에만 있다
(예: "최종이자율 (2026.09.29 현재, 세전) 연 3.25%"). 기간별이면 가장 짧은 가입 기간 값이고 나머지 기간 값은 quote에 있다.
2026-11-17 조건표 기준 13개 중 6개에 있다.

- maxRate는 우대 조건을 모두 충족했을 때의 원문 값이다(원문 단서 "※ 우대 이자율 최고 연 X% 적용 시"). 받을 수 있는 금리로 표시하지 않는다.
- 화면은 maxRate를 표시할 때 반드시 "우대 조건 충족 시" 문구를 함께 쓴다. 우대 조건 목록은 이 API가 제공하지 않는다.
- 확인: 2026-11-17 maxRate 6개를 PDF 원문 칸('최고/최종이자율')과 대조, 6/6 일치(줄바꿈→공백 정규화).

- 금리는 원문 값을 옮길 뿐이다. 기본금리에 우대금리를 **더하거나 계산해서 만든 숫자는 내지 않는다**.
- 원문에서 근거를 찾지 못한 값은 null이다(지어내지 않음). 화면은 null이면 금리를 표시하지 않는다.

### 2-3. excludedSummary

- 제외 상품은 이름·id 없이 **이유별 개수만** 준다. 상품 하나는 이유 하나로만 센다(`count` 합 = 제외 상품 수,
  `products` 수 + `count` 합 = 조건표 상품 수).
- 이유는 추천기 판정 이름을 그대로 쓴다. 상품이 어긴 판정 중 아래 순서에서 처음 걸린 것 하나다.

| 순서 | reason | 뜻 |
|---|---|---|
| 1 | recommendable | 추천 대상에서 뺀 상품(조건표 `recommendable=false`) |
| 2 | kind | 요청한 `productType`과 다름 |
| 3 | foreigner | 원문상 외국인 가입 불가 |
| 4 | visa | 비자 종류가 가입 대상이 아님 |
| 5 | age | 나이가 가입 범위 밖 |
| 6 | stayTerm | 가장 짧은 계약 기간이 남은 체류보다 김 |
| 7 | spare | 적금 월 최소 납입액이 월 여유자금보다 큼 |
| 8 | requestTerm | 요청 `maxTermMonths`보다 김, 또는 기간 근거가 없어 확인 불가 |
| 9 | requestMonthly | 요청 `monthlyAmountKrw`가 납입 범위 밖·여유자금 초과, 또는 근거가 없어 확인 불가 |
| 10 | savingType | 요청 `accrual`과 다름, 또는 근거가 없어 확인 불가 |
| 11 | requireForeignerExplicit | 외국인 명시 상품만 요청했는데 원문 명시가 아님 |

- 결과에 나온 이유만, 위 순서로 준다.

## 3. 오류

팀 공통 형식 `{code, message, details, requestId}`.

| 상태 | code | 뜻 |
|---|---|---|
| 400 | `VALIDATION_ERROR` | 본문 검증 실패. `details.fields`에 필드 경로와 사유(제출 값은 되돌려 보내지 않음) |
| 401 | `UNAUTHORIZED` | 내부 토큰 없음·불일치(AI에 토큰 설정이 없을 때도) |
| 500 | `INTERNAL_ERROR` | 그 밖의 오류 |

- 검색 실패는 오류가 아니다(순서 A로 답하고 `rankedBy=catalog_search_failed`).

## 4. 한계

- 금리는 상품설명서 PDF(`C:/Projects/hanstep/rag-data/products`)의 기준일 값이다. 고시 금리가 바뀌어도 조건표를
  다시 채우기 전에는 반영되지 않는다. 화면은 `baseRate.asOf`를 함께 보여 줘야 한다.
- 조건표 상품 13개 밖의 상품은 추천하지 않는다.

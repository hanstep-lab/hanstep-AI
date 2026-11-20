# 금융상품 추천 카드 `PRODUCT_RECOMMEND_CARD` 계약 v1 (#114, 프론트 계약)

챗봇 상품 추천 도구 `recommend_savings_products`의 결과를 화면 카드로 보여 준다(chat.done `blocks`).
형식은 환율 진단 카드(`specs/chat/fx_summary_tool_v1.md` "화면 카드" 절)와 같은 방식으로 정했다. blocks 공통 규칙은 `specs/chat/chat_blocks_v1.md` 2절을 따른다.

- 상태: **계약 초안(구현 전)**. 이 문서를 FE와 맞춘 뒤 구현한다(#114 1단계).
- 기준 커밋: hanstep-AI `origin/develop` `91c33ad`, hanstep-backend PRD-02 `ProductRecommendResponse`(hanstep-backend `src/main/java/com/hanstep/product/dto/ProductRecommendResponse.java`).

## 화면 카드(chat.done blocks, `PRODUCT_RECOMMEND_CARD`)

도구가 `SUCCEEDED`이면 AI는 `agent.done`의 `blocks`에 상품 추천 카드를 넣는다. Spring은 이 값을 `chat_messages.blocks_json`에 저장하고 `chat.done`으로 그대로 보낸다(대화 기록 재조회도 같은 값). Spring 코드는 바꾸지 않는다(blocks를 JSON 그대로 중계한다, `chat_blocks_v1.md` 5절).

- 숫자는 코드가 추천기 결과에서만 채운다. LLM이 쓴 숫자는 넣지 않는다. LLM에 주는 결과(`product_tools.py`의 `content`)는 카드와 상관없이 그대로다.
- 카드는 LLM용 결과가 아니라 추천기 원본으로 만든다. 그래서 LLM에 주지 않는 금리(`baseRate`·`maxRate`)·상품 특징(`feature`)·상품 id가 있다.
- 필드 이름은 금융상품 추천 화면 API(PRD-02 `GET /api/v1/finance/products/recommend`, `ProductRecommendResponse`)와 같다.
  - AI 쪽에서는 화면 추천 API(`POST /internal/v1/products/recommend`, `specs/products/recommend_api_v1.md`)의 `screen_item()` 결과를 그대로 쓴다.
  - 그중 `rankedBy`(순서 출처, 내부용)·`isMock`(PRD-02에서 Spring이 더하는 값)은 넣지 않는다.
- 금리는 상품설명서 원문 값만 옮긴다. 우대금리를 더하거나 계산한 숫자는 없다. 근거가 없으면 null이고, 화면은 금리를 표시하지 않는다.
  - `baseRate`는 `baseRate.asOf`와 함께 표시한다.
  - `maxRate`는 **우대 조건 충족 시** 최고 금리다. 화면은 '우대 조건 충족 시' 문구와 함께 표시한다.
- 문장(추천 이유·권유)과 버튼 경로는 넣지 않는다.
- `FAILED`·`REJECTED`·`TIMEOUT`이면 카드가 없다. 카드 값만 형식이 어긋나면 답변은 그대로 두고 카드만 내지 않는다.
- 켜지는 설정: 프롬프트 **v4.5**에서만 만든다(v4.4 이하에는 카드가 없다). 기본 프롬프트 버전은 바꾸지 않는다.

### 상품은 LLM이 받은 도구 결과 그대로(최대 5개)

- `products`는 LLM이 받은 도구 결과의 상품을 같은 순서로 전부 담는다(외국인 가입 원문 명시 상품 먼저, 그다음 검색 또는 조건표 순서). 최대 5개다(`app/products/recommender.py` `TOP_K`).
- LLM이 받는 개수는 바꾸지 않는다(상품 추천 측정 조건 유지). 그래서 답변이 말한 상품은 모두 카드에 있다.
- 프롬프트 v4.6(#119)의 답변 본문은 카드에 있는 상품 이름·기간·금액 범위·금리를 다시 나열하지 않는다. 결론 한 줄(맞는 상품 수와 주된 이유), 카드에 없는 내용(확인하지 못한 조건, 금리·우대 조건은 카드에 있다는 안내), 출처 줄만 쓴다(`specs/chat/answer_display_v1.md` 규칙 6). v4.5 답변은 그대로다.
- 화면에 몇 개를 보여 줄지는 앱이 정한다(아래 "앱 처리").

### 기권 카드(`products: []`)

- 조건을 통과한 상품이 없어도 도구는 `SUCCEEDED`다. 이때 카드는 `products: []`와 `abstainReason`을 담는다(PRD-02 기권 응답과 같은 모양).
- `abstainReason`
  - `REQUEST_EXCEEDS_SURPLUS`: 요청 월 납입액이 여유자금보다 크다.
  - `STAY_TOO_SHORT`: 남은 체류보다 짧은 상품이 없다.
  - `NO_MATCHING_PRODUCT`: 조건에 맞는 상품이 없다.
- `excludedSummary`(이유별 제외 상품 수)로 왜 없는지 보여 줄 수 있다. 상품 이름·id는 없다.

### 한 턴에 카드 하나(마지막 성공 호출)

한 턴에 `recommend_savings_products`를 여러 번 불러도(예: 적금 → 예금) 카드는 1장이다.

- 마지막으로 `SUCCEEDED`한 호출의 결과로 만든다. 답변은 보통 마지막 조회 결과를 말하기 때문이다.
- 카드 자리는 그 호출의 자리(도구 실행 순서)다.
- 실패한 호출은 카드를 만들지도 바꾸지도 않는다.

### 필드

| 필드 | 뜻 | null |
|---|---|---|
| `type` | `"PRODUCT_RECOMMEND_CARD"` | |
| `asOf` | 판정 기준일(Asia/Seoul, `YYYY-MM-DD`) | |
| `remainingStayMonths` | `asOf`부터 체류 만료일까지 채운 개월 수 | 만료일이 없으면 null |
| `abstainReason` | `products`가 비었을 때만 이유 코드(위 절) | 상품이 있으면 null |
| `products[]` | 조건을 통과한 상품, LLM이 받은 순서 그대로 최대 5개. 기권이면 `[]` | |
| `products[].productId` · `nameKo` | 상품 id(상품설명서 문서 id) · 상품명(한국어 원문) | |
| `products[].kind` | `SAVINGS`(적금) / `DEPOSIT`(정기예금) | |
| `products[].termMonths` | 계약 기간(개월). 정해진 기간 중 고르면 정수 배열(예: `[6, 12]`), 범위형은 `{min, max}` | 근거가 없으면 null |
| `products[].monthlyAmountKrw` · `depositAmountKrw` | 적금 월 납입 범위 · 예금 가입 금액 범위 `{min, max}`(원). `max`는 원문이 '제한없음'이면 문자열 `"no limit"` | 해당 종류가 아니면 null, 근거가 없으면 null |
| `products[].accrual` | 적금 적립 방식 `FLEXIBLE`(자유) / `FIXED`(정액) | 예금이면 null |
| `products[].foreignerEligible` | 외국인 가입: `explicit`(원문 명시) / `inferred`(해석) | 근거가 없으면 null |
| `products[].notChecked` | 프로필 값·근거가 없어 판정하지 못한 조건: `visa` / `age` / `remainingStay` / `spareMoney` | 없으면 `[]` |
| `products[].stayFits` | 가장 짧은 계약 기간이 남은 체류 개월 이하면 true | 판정 불가면 null |
| `products[].baseRate` | 기본금리 `{ratePct, asOf, quote, page}`. `ratePct`는 원문 연 이율(%), 기간별이면 가장 짧은 기간의 값. `asOf`는 원문 금리 기준일(없으면 null) | 근거가 없으면 null |
| `products[].maxRate` | 우대 조건 충족 시 최고 금리 `{ratePct, quote, page}` | 원문에 최고(최종) 이자율이 없으면 null |
| `products[].feature` | 원문 '상품특징' 인용 `{quote, page}` | 근거가 없으면 null |
| `products[].evidence[]` | 판정에 쓴 원문 근거 인용 `{about, quote, page}`, 최대 4개. `about`은 `term` / `amount` / `accrual` / `foreigners` | 없으면 `[]` |
| `excludedSummary[]` | 제외된 상품의 이유별 개수 `{reason, count}`. `reason`은 추천기 판정 이름(`recommendable`·`kind`·`foreigner`·`visa`·`age`·`stayTerm`·`spare`·`requestTerm`·`requestMonthly`·`savingType`·`requireForeignerExplicit`) | 제외 상품이 없으면 `[]` |

- 키는 늘 같다. 값이 없으면 null로 둔다(PRD-02와 같은 규칙).
- 금액·금리 숫자는 원문 그대로다. 앱은 계산하지 않고 표시만 한다.

### 예시 1: 성공 (상품 3개)

현재 추천기(`app/products/screen.py`의 `recommend_for_screen`, 순서 A = 조건표)를 그대로 실행한 출력에서 카드 필드만 남겼다. LLM 호출은 없다.
조건: E-9, 만 30세, 체류 만료 2027-10-04(남은 10개월), 월 여유자금 675,317원, 기준일 2026-11-20, 조건 `productType=SAVINGS`.
조건을 통과한 상품이 3개라 3개다. 두 번째 상품은 `maxRate`가 null이고(원문에 최고 이자율 없음), 세 번째 상품은 `monthlyAmountKrw`가 null이다(원문에 월 납입 범위 없음, `notChecked`에 `spareMoney`).

```json
[
  {
    "type": "PRODUCT_RECOMMEND_CARD",
    "asOf": "2026-11-20",
    "remainingStayMonths": 10,
    "abstainReason": null,
    "products": [
      {
        "productId": "doc-de733331abd3",
        "nameKo": "신한 땡겨요 적금",
        "kind": "SAVINGS",
        "termMonths": {
          "min": 6,
          "max": 6
        },
        "monthlyAmountKrw": {
          "min": 1000,
          "max": 300000
        },
        "depositAmountKrw": null,
        "accrual": "FLEXIBLE",
        "foreignerEligible": "inferred",
        "notChecked": [
          "visa",
          "age"
        ],
        "stayFits": true,
        "baseRate": {
          "ratePct": 2.2,
          "asOf": "2026-10-01",
          "quote": "기본이자율 (2026.10.01 현재, 세전) 연 2.20%",
          "page": 1
        },
        "maxRate": {
          "ratePct": 3.7,
          "quote": "최고이자율 (2026.10.01 현재, 세전) 최고 연 3.70%",
          "page": 2
        },
        "feature": {
          "quote": "신한은행 배달앱 “땡겨요” 서비스를 이용하거나 주단위 적금 납입 미션 달성 고객에게 우대금리를 제공하는 적립식 상품",
          "page": 1
        },
        "evidence": [
          {
            "about": "term",
            "quote": "계약기간 6개월",
            "page": 1
          },
          {
            "about": "amount",
            "quote": "최소 1천원 이상 월 30만원 이하",
            "page": 1
          },
          {
            "about": "accrual",
            "quote": "상품유형 자유적립식",
            "page": 1
          },
          {
            "about": "foreigners",
            "quote": "가입대상 실명의 개인 및 개인사업자",
            "page": 1
          }
        ]
      },
      {
        "productId": "doc-ad95d3f72e39",
        "nameKo": "정기적금",
        "kind": "SAVINGS",
        "termMonths": {
          "min": 1,
          "max": 60
        },
        "monthlyAmountKrw": {
          "min": 10000,
          "max": "no limit"
        },
        "depositAmountKrw": null,
        "accrual": "FIXED",
        "foreignerEligible": "inferred",
        "notChecked": [
          "visa",
          "age"
        ],
        "stayFits": true,
        "baseRate": {
          "ratePct": 2.2,
          "asOf": "2026-09-03",
          "quote": "기본이자율 (2026.09.03 현재, 세전) 가입기간 1개월이상 3개월미만 3개월이상 6개월미만 6개월이상 12개월미만 12개월이상 24개월미만 정기적립식 연 2.20% 연 2.25% 연 2.30% 연 2.45% 가입기간 24개월이상 36개월미만 36개월 37개월이상 60개월이하 정기적립식 연 2.55% 연 2.55% 연 2.70%",
          "page": 1
        },
        "maxRate": null,
        "feature": {
          "quote": "일정 기간 후에 일정 금액을 지급할 것을 미리 약정하고 월 1회 일정한 일자에 일정금액을 수입하는 예금",
          "page": 1
        },
        "evidence": [
          {
            "about": "term",
            "quote": "1개월 이상 60개월 이하(월단위)",
            "page": 3
          },
          {
            "about": "amount",
            "quote": "최저가입금액 1만원 이상",
            "page": 5
          },
          {
            "about": "accrual",
            "quote": "상품유형 정기적립식",
            "page": 3
          },
          {
            "about": "foreigners",
            "quote": "가입대상 제한없음",
            "page": 5
          }
        ]
      },
      {
        "productId": "doc-a5851b226169",
        "nameKo": "한 달부터 적금",
        "kind": "SAVINGS",
        "termMonths": {
          "min": 1,
          "max": 12
        },
        "monthlyAmountKrw": null,
        "depositAmountKrw": null,
        "accrual": "FLEXIBLE",
        "foreignerEligible": "inferred",
        "notChecked": [
          "visa",
          "age",
          "spareMoney"
        ],
        "stayFits": true,
        "baseRate": {
          "ratePct": 1.8,
          "asOf": "2026-09-03",
          "quote": "기본이자율 (2026.09.03 현재, 세전) 연 1.80%",
          "page": 2
        },
        "maxRate": {
          "ratePct": 3.8,
          "quote": "최고이자율 (2026.09.03 현재, 세전) 연 3.80%",
          "page": 2
        },
        "feature": {
          "quote": "1 개월 이상 12 개월 이내 가입 가능한 상품으로, 납입 주기에 따른 총 납입 회차에 대해 일정 회차 이상 납입 시 우대이자율을 제공하는 적립식 예금",
          "page": 1
        },
        "evidence": [
          {
            "about": "term",
            "quote": "계약기간 1개월 이상 12개월 이하",
            "page": 1
          },
          {
            "about": "accrual",
            "quote": "상품유형 자유적립식",
            "page": 1
          },
          {
            "about": "foreigners",
            "quote": "가입대상 실명의 개인 및 개인사업자",
            "page": 1
          }
        ]
      }
    ],
    "excludedSummary": [
      {
        "reason": "recommendable",
        "count": 1
      },
      {
        "reason": "kind",
        "count": 4
      },
      {
        "reason": "stayTerm",
        "count": 5
      }
    ]
  }
]
```

### 예시 2: 기권 (`REQUEST_EXCEEDS_SURPLUS`)

같은 사용자가 "매달 100만 원씩 넣을 적금"을 물은 경우다(조건 `productType=SAVINGS`, `monthlyAmountKrw=1000000`, 여유자금 675,317원). 같은 추천기 실행 결과다.

```json
[
  {
    "type": "PRODUCT_RECOMMEND_CARD",
    "asOf": "2026-11-20",
    "remainingStayMonths": 10,
    "abstainReason": "REQUEST_EXCEEDS_SURPLUS",
    "products": [],
    "excludedSummary": [
      {
        "reason": "recommendable",
        "count": 1
      },
      {
        "reason": "kind",
        "count": 4
      },
      {
        "reason": "stayTerm",
        "count": 5
      },
      {
        "reason": "requestMonthly",
        "count": 3
      }
    ]
  }
]
```

## 앱 처리(합의할 것)

- 지금 앱은 모르는 `type`을 버린다(`chat_blocks_v1.md` 6.2). 그래서 앱 업데이트 전에 이 카드가 와도 화면이 깨지지 않는다.
- 표시 개수는 앱 몫이다. 앞 3개를 보여 주고 나머지는 '더 보기'로 펼친다(`products`는 최대 5개).
- 카드에는 고정 안내 문구 **'상품설명서 기준 · 모의 추천, 가입 승인 아님'**을 앱이 표시한다(블록에는 문장을 넣지 않는다).
- 버튼(상품 상세·추천 화면 이동)은 앱이 고정으로 붙인다. 상품 상세로 갈 때는 `productId`를 쓴다.
- 상품명(`nameKo`)과 원문 인용(`quote`)은 한국어 원문이다. 번역해서 보여 줄지는 앱이 정한다.

## 측정 영향

LLM에 주는 결과와 측정 값(`agent.metrics`)은 그대로다. 측정 스크립트(`scripts/evaluate_chat.py`)의 숫자 검사는 LLM에 준 값만 근거로 쓰고, 이 카드는 근거에서 뺀다(LLM이 받지 않은 금리가 근거로 들어가지 않게).
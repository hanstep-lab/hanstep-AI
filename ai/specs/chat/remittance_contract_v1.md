# 챗봇 송금 도구 ↔ Spring 내부 API 계약 v1

챗봇 송금 조회 도구(`app/chat/remittance_tools.py`)가 부르는 Spring 내부 API 세 개의 요청·응답 형식이다.
AI 쪽에서 정한 초안이며, Spring 구현 때 이 문서에 다시 맞춘다.

- 인증: 다른 내부 API와 같이 `X-Internal-Token` 헤더, 요청 추적용 `X-Request-Id`.
- 모든 호출은 조회만 한다. 송금을 만들거나 실행하지 않는다.
- 금액은 정수(소수 없음), 통화는 ISO 4217 코드다. 금액 계산(견적)은 Spring만 한다. AI는 견적 결과를 옮겨 쓴다.
- AI는 재시도하지 않는다(도구 시간 예산 `CHAT_TOOL_TIMEOUT_SECONDS`).
- Spring 설정이 없거나 `CHAT_LLM_MODE=fake`이면 AI는 `tests/fixtures/chat/remittance_context.json` 값을 쓴다.

## 공통 객체

### recipient (저장된 받는 분)

| 필드 | 형식 | 예 |
|---|---|---|
| recipientId | long, 1 이상 | 11 |
| nameEn | string | `LORNA REYES` |
| relationship | string(`MOTHER`, `SELF` 등) | `MOTHER` |
| countryCode | ISO 3166-1 alpha-2 | `PH` |
| currency | ISO 4217, 받는 분이 받는 통화 | `PHP` |
| bankName | string | `BDO` |
| accountLast4 | 숫자 4자리 문자열 | `"5566"` |

계좌 번호 전체는 주지 않는다.

### appliedRate (참고 환율)

`unit` 외화 = `rate` KRW.

| 필드 | 형식 | 예 |
|---|---|---|
| currency | ISO 4217 | `PHP` |
| unit | int, 1 이상 | 1 |
| rate | number | 22.32 |
| sourceLabel | string | `ECB 기준환율 기반 참고값` |
| asOf | date(`yyyy-MM-dd`) | `2026-11-06` |
| isMock | boolean | true |

AI는 `isMock`을 LLM에 주지 않고, 확인 카드에는 그대로 싣는다.
LLM에 줄 때는 AI가 `rateText`(`"{unit} {currency} = {rate} KRW"`, 예: `1 PHP = 22.32 KRW`, `100 VND = 5.43 KRW`)를 붙인다. Spring 응답과 확인 카드에는 없는 필드다.

### money

`{"amount": 500000, "currency": "KRW"}`

## 1. 받는 분 목록

`GET /internal/v1/ai-context/users/{userId}/remittance-recipients`

200:

```json
{
  "recipients": [
    {"recipientId": 11, "nameEn": "LORNA REYES", "relationship": "MOTHER", "countryCode": "PH",
     "currency": "PHP", "bankName": "BDO", "accountLast4": "5566"}
  ]
}
```

받는 분이 없으면 `recipients: []`.

## 2. 참고 환율

`GET /internal/v1/ai-context/fx-rates?currency=PHP`

200: appliedRate 객체 하나.

```json
{"currency": "PHP", "unit": 1, "rate": 22.32, "sourceLabel": "ECB 기준환율 기반 참고값",
 "asOf": "2026-11-06", "isMock": true}
```

404 `FX_RATE_NOT_FOUND`: 그 통화의 환율이 없음.

## 3. 송금 견적

`GET /internal/v1/ai-context/users/{userId}/remittance-quote?recipientId=11&amount=500000&currency=KRW`

- Spring INT-18(`RemittanceInternalController`). 응답은 앱 API `GET /api/v1/transfers/quote`(BE #122)와 같은 `RemittanceQuoteResponse`다. 잔액은 보지 않는다.
- `currency=KRW`: `amount`는 **수수료를 포함한 출금 총액**(내 계좌에서 나갈 돈). 송금은 USD로 나간다.
- `currency`=받는 분 통화(예: `PHP`): BE #122 이후 Spring이 400 `VALIDATION_ERROR`로 거절한다(KRW만 받음).

200(운영 실제 응답, Reyes, 500,000원, 2026-11-19. `sources`는 일부만):

```json
{
  "recipientId": 1,
  "sendAmount": {"amount": 500000, "currency": "KRW"},
  "receiveAmount": {"amount": 23587, "currency": "PHP"},
  "appliedRate": {"currency": "PHP", "unit": 1, "rate": 20.926,
                  "sourceLabel": "ECB 기준환율 기반 참고값", "asOf": "2026-11-18", "isMock": false},
  "isReference": true,
  "referenceNotice": "LOCAL_PAYOUT_RATE_DIFFERS",
  "sendCurrency": "USD",
  "defaultProduct": "SOL_EASY",
  "isEstimated": true,
  "rates": {"usdBaseRate": 1331.1, "usdRateDate": "2026-11-19", "usdSource": "KOREAEXIM",
            "phpRefRate": 20.926, "phpRateDate": "2026-11-18", "phpSource": "frankfurter-ecb",
            "usdPhpRefRate": 63.6099},
  "quotes": [
    {"product": "SOL_EASY", "available": true, "unavailableReason": null,
     "sendAmountUsd": 370.81, "spreadPct": 0.954, "preferentialPct": 70, "remittanceCount1y": 23,
     "usdAppliedRate": 1334.91, "krwPrincipal": 494998, "fxMarginKrw": 1413,
     "sendFeeKrw": 0, "telegraphicFeeKrw": 5000, "feeUsd": null, "totalWithdrawKrw": 499998,
     "unusedKrw": null, "recipientAmountUsd": 370.81, "recipientAmountPhpRef": 23587.19,
     "deliveryType": "SWIFT_ACCOUNT",
     "notices": ["INTERMEDIARY_FEE_MAY_APPLY", "LOCAL_PAYOUT_RATE_DIFFERS",
                 "FOREIGN_EXCHANGE_BANK_DESIGNATION_REQUIRED"],
     "sources": [{"item": "telegraphicFeeKrw", "title": "신한 SOL 쏠편한 해외송금 안내",
                  "approvalNo": "준법감시인 사전심사필 제2026-14794-1호(2026.09.16~2027.09.15)",
                  "checkedOn": "2026-11-18"}]},
    {"product": "SOL_FAST", "available": true, "feeUsd": 3.5, "sendFeeKrw": null, "...": "..."}
  ]
}
```

전체 응답은 `tests/fixtures/chat/remittance_quote_reyes_500000.json`(불가 예는 `_4700.json`)에 있다. 필드 설명은 hanstep-backend `RemittanceQuoteResponse`(`RemittanceProductQuote`, `RemittanceQuoteRates`, `RemittanceQuoteSource`)를 따른다.

AI가 쓰는 값(#105)
- `quotes[]`에서 `product == defaultProduct`인 상품 하나(지금은 늘 `SOL_EASY`)만 쓴다. 다른 상품은 쓰지 않는다.
- 숫자는 Spring 값 그대로 쓴다. AI는 계산하지 않는다.
- null이 될 수 있는 값
  - `receiveAmount`·`appliedRate`: PHP 참고 환율이 없거나(국적이 PHL이 아님, ECB 값 없음) 기본 상품이 보낼 수 없을 때. PHP 참고 환율이 없으면 `rates.php*`도 null이다
  - `sendFeeKrw`·`telegraphicFeeKrw`: 쏠빠른, 또는 쏠편한이 보낼 수 없을 때
  - `feeUsd`: 쏠편한, 또는 쏠빠른이 보낼 수 없을 때
  - `remittanceCount1y`: 쏠빠른
  - `sendAmountUsd`·`krwPrincipal`·`fxMarginKrw`·`totalWithdrawKrw`·`recipientAmountUsd`: 그 상품이 보낼 수 없을 때
  - `unusedKrw`: `AMOUNT_CAPPED_AT_FEE_BOUNDARY`일 때만 값이 있다
- `fxMarginKrw`는 `krwPrincipal`에 이미 들어 있는 참고값이다(출금 총액에 더하지 않는다).

| 상태 | code | 뜻 |
|---|---|---|
| 404 | `RECIPIENT_NOT_FOUND` | 그 사용자의 저장된 받는 분이 아님 |
| 400 | `VALIDATION_ERROR` | 금액이 1 미만이거나 통화가 KRW가 아님 |
| 404 | `FX_RATE_NOT_FOUND` | USD 매매기준율(KOREAEXIM)이 없음 |

AI 처리
- 견적의 `recipientId`, 보낼 통화(KRW), 요청한 금액이 요청과 다르면 카드를 내지 않는다(FAILED `INVALID_RESPONSE`). `receiveAmount`·`appliedRate`는 있을 때만 통화를 본다.
- `defaultProduct`가 있는데 그 상품이 `quotes[]`에 없거나 `rates`가 없으면 FAILED `INVALID_RESPONSE`.
- 기본 상품이 `available=false`면 카드 없이 REJECTED `QUOTE_UNAVAILABLE`. LLM에는 `unavailableReason`과 영어 이유를 준다.

| unavailableReason | 뜻 | LLM에 주는 이유 |
|---|---|---|
| `AMOUNT_TOO_SMALL` | 수수료를 빼면 보낼 돈이 0 이하(예: 4,700원) | The amount is too small: nothing is left to send after the fees. |
| `LIMIT_EXCEEDED` | 쏠빠른 건당 USD 5,000 초과 | The amount is over this product's per-transfer limit. |
| `FEE_TABLE_UNAVAILABLE` | 쏠편한 수수료 이벤트 종료 | This product's fee table is not available now. |

### LLM에 주는 견적(#105)

`quote_remittance`가 SUCCEEDED면 LLM에 아래 값을 준다(`app/chat/remittance_tools.py` `_quote_for_llm`). 값은 Spring 그대로이고, 코드는 환율 문장(`rateText`)과 영어 안내만 붙인다.

| 키 | 내용 |
|---|---|
| `recipient` | 받는 분 |
| `sendAmount` | 출금 총액(수수료 포함, KRW) |
| `product`, `deliveryType`, `sendCurrency` | `SOL_EASY`·`SWIFT_ACCOUNT`·`USD` 등 |
| `sendAmountUsd`, `recipientAmountUsd` | 보내는 USD, 받는 USD(중계·수취은행 수수료 차감 전) |
| `remittanceRate` | 송금 환율 `{rate, rateText: "1 USD = 1,334.91 KRW"}` |
| `usdBaseRate` | USD 매매기준율 `{rate, source, asOf}` |
| `fees` | `sendFeeKrw`, `telegraphicFeeKrw`, `feeUsd`, `fxMarginKrw`, `totalWithdrawKrw`, `unusedKrw` |
| `preferentialPct`, `remittanceCount1y` | 우대율(%), 우대 구간을 정한 1년 송금 횟수 |
| `referenceAmount` | PHP 참고 금액 `{amount, currency, rate: {…, rateText: "1 PHP = 20.926 KRW", source}, note}`. 없으면 null. **`appliedRate`라는 이름으로 주지 않는다** |
| `notices` | `[{code, text}]`. text는 영어 설명 |
| `sources` | 기본 상품의 출처 `{item, title, approvalNo, checkedOn, sourceName}`. title·approvalNo는 한국어 원문, sourceName은 답변에 쓸 짧은 영어 이름 |
| `answerGuide` | 이 견적에 맞춘 영어 답변 안내(아래) |
| `note` | 카드가 화면에 나간다는 한 줄 |

`answerGuide`(견적마다 코드가 만든다. 숫자는 그 견적 값)
- 출금 총액에 수수료가 포함돼 있다고 말한다.
- 0원이 아닌 수수료는 금액을 말한다(예: a KRW 5,000 telegraphic fee). 환전 마진은 "about KRW …"로, 환율에 들어 있다고 말한다.
- 송금 환율은 `1 USD = … KRW` 하나다. PHP 환율은 참고 금액용이라 송금 환율·적용 환율이라고 하지 않는다.
- 받는 금액은 "about … PHP (reference)". PHP 참고 환율이 없으면 받는 USD를 말한다.
- `INTERMEDIARY_FEE_MAY_APPLY`가 있으면 중계은행 수수료가 더 붙을 수 있다고 금액 없이 말한다.
- 우대율을 말하면 근거를 붙인다. `remittanceCount1y` 출처가 Mock이면 "the user's sample remittance history (an estimate)". 도구 결과에 없는 우대는 말하지 않는다.
- 출처는 영어로 짧게(`sourceName`). 한국어 문서 제목·심사필 번호는 옮기지 않는다.
- "live·real-time"이라고 하지 않는다.

프롬프트 본문(`prompts/remittance_rules.md`)은 바꾸지 않았다. 이 값과 안내는 송금 도구가 붙는 모든 버전(v3~v4.4)에 같이 간다.

### fixture 견적 규칙(fake 모드·테스트 전용)

운영 견적은 Spring이 정한다. fixture는 아래처럼 계산한다.

- 보낼 KRW → 받을 금액: `floor(amount ÷ (rate ÷ unit))`. 500,000 KRW → 22,401 PHP
- 받을 금액 → 보낼 KRW: `ceil(amount × (rate ÷ unit))`. 20,000 PHP → 446,400 KRW

## 확인 카드(chat.done blocks)

견적이 성공하면 AI는 `agent.done`의 `blocks`에 카드 하나를 넣는다(Spring은 `chat_messages.blocks_json`에 저장, 화면이 그린다).
숫자는 견적 결과에서만 채운다. 송금은 사용자가 송금 화면에서 확인하고 실행한다.

**카드에는 코드·숫자만 넣는다(#105).** 제목·항목 이름·안내 문구·출처 문구는 앱이 화면 언어로 만든다. 한국어로만 있는 값(`sources[].title`, `approvalNo`)은 카드에 넣지 않는다.
- 예외: `appliedRate.sourceLabel`(한국어)은 하위 호환으로 그대로 둔다. 앱은 이 값 대신 `rateSources`로 문구를 만든다.

운영 실제 견적(Reyes, 500,000원)으로 만든 카드:

```json
{
  "type": "REMITTANCE_CONFIRM",
  "recipient": {"recipientId": 1, "nameEn": "LORNA REYES", "relationship": "MOTHER",
                "countryCode": "PH", "currency": "PHP", "bankName": "BDO", "accountLast4": "5566"},
  "sendAmount": {"amount": 500000, "currency": "KRW"},
  "receiveAmount": {"amount": 23587, "currency": "PHP"},
  "appliedRate": {"currency": "PHP", "unit": 1, "rate": 20.926,
                  "sourceLabel": "ECB 기준환율 기반 참고값", "asOf": "2026-11-18", "isMock": false},
  "transferPath": "/transfer?recipientId=1&amount=500000",
  "fees": {"product": "SOL_EASY", "sendFeeKrw": 0, "telegraphicFeeKrw": 5000, "feeUsd": null,
           "fxMarginKrw": 1413, "totalWithdrawKrw": 499998, "preferentialPct": 70,
           "usdAppliedRate": 1334.91,
           "notices": ["INTERMEDIARY_FEE_MAY_APPLY", "LOCAL_PAYOUT_RATE_DIFFERS",
                       "FOREIGN_EXCHANGE_BANK_DESIGNATION_REQUIRED"]},
  "rateSources": {"usd": "KOREAEXIM", "php": "frankfurter-ecb"}
}
```

- `transferPath`의 `amount`는 보낼 원화 금액(`sendAmount.amount`)이다.
- 기존 필드(`recipient`, `sendAmount`, `receiveAmount`, `appliedRate`, `transferPath`)는 이름·모양·값을 바꾸지 않았다(#105는 `fees`·`rateSources` 추가만).
- `receiveAmount`·`appliedRate`는 PHP 참고 환율이 없을 때만 null이다(#105 전에는 이 경우 카드가 없었다).
- 기본 상품이 보낼 수 없는 금액(예: 4,700원, `AMOUNT_TOO_SMALL`)이면 카드가 없다.

`fees`(기본 상품, Spring 값 그대로)

| 필드 | 타입 | null | 뜻 |
|---|---|---|---|
| `product` | `SOL_EASY` \| `SOL_FAST` | 아니오 | 상품 코드 |
| `sendFeeKrw` | int | 예(쏠빠른) | 송금수수료(원). 쏠편한은 USD 5,000 이하 0원 |
| `telegraphicFeeKrw` | int | 예(쏠빠른) | 전신료(원) |
| `feeUsd` | number | 예(쏠편한) | 송금수수료(USD). 쏠빠른 3.5 |
| `fxMarginKrw` | int | 아니오(보낼 수 있을 때) | 환전 마진(원). 송금 환율에 이미 들어 있어 출금 총액에 더하지 않는다 |
| `totalWithdrawKrw` | int | 아니오(보낼 수 있을 때) | 출금 총액(원). 요청 금액을 넘지 않는다 |
| `preferentialPct` | number | 아니오 | 환율 우대율(%). 쏠빠른 0 |
| `usdAppliedRate` | number | 아니오 | 송금 환율(1 USD 당 원) |
| `notices` | string[] | 아니오 | 안내 코드(아래) |

`rateSources`

| 필드 | 값 | 뜻 |
|---|---|---|
| `usd` | `KOREAEXIM` | USD 매매기준율 출처(한국수출입은행) |
| `php` | `frankfurter-ecb` \| `MOCK`(로컬 Mock 환율만) \| null | PHP 참고 환율 출처(ECB 기준환율). PHP 참고 환율이 없으면 null |

`notices` 코드(Spring `RemittanceQuoteCalculator`)

| 코드 | 붙는 곳 | 뜻 |
|---|---|---|
| `INTERMEDIARY_FEE_MAY_APPLY` | 쏠편한 | 중계·수취은행 수수료가 붙을 수 있음(금액 없음) |
| `LOCAL_PAYOUT_RATE_BY_AGENT` | 쏠빠른 | 현지 지급 환율은 지급 취급점이 정함 |
| `LOCAL_PAYOUT_RATE_DIFFERS` | PHP 참고 환율이 있을 때 | 받는 금액은 현지 환율로 바뀜(참고값) |
| `PHP_REF_RATE_UNAVAILABLE` | PHP 참고 환율이 없을 때 | PHP 참고 환율 없음 |
| `FEE_TABLE_UNAVAILABLE` | 쏠편한 수수료 이벤트 종료 | 수수료표 없음 |
| `AMOUNT_CAPPED_AT_FEE_BOUNDARY` | 쏠편한 수수료 경계 | USD 5,000.00까지만 보내고 나머지는 남음(`unusedKrw`) |
| `FOREIGN_EXCHANGE_BANK_DESIGNATION_REQUIRED` | 늘 | 거래외국환은행 지정 필요 |

fake 모드(fixture 견적)는 상품 견적이 없어 `fees`·`rateSources`가 null이다. 운영(Spring)에서는 늘 있다.

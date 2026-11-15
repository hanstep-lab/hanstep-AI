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

- `currency=KRW`: `amount`는 보낼 원화 금액.
- `currency`=받는 분 통화(예: `PHP`): `amount`는 받는 분이 받을 금액.

200:

```json
{
  "recipientId": 11,
  "sendAmount": {"amount": 500000, "currency": "KRW"},
  "receiveAmount": {"amount": 22401, "currency": "PHP"},
  "appliedRate": {"currency": "PHP", "unit": 1, "rate": 22.32,
                  "sourceLabel": "ECB 기준환율 기반 참고값", "asOf": "2026-11-06", "isMock": true}
}
```

| 상태 | code | 뜻 |
|---|---|---|
| 404 | `RECIPIENT_NOT_FOUND` | 그 사용자의 저장된 받는 분이 아님 |
| 400 | `VALIDATION_ERROR` | 금액이 1 미만이거나 통화가 KRW·받는 분 통화가 아님 |
| 404 | `FX_RATE_NOT_FOUND` | 받는 분 통화의 환율이 없음 |

AI는 견적의 `recipientId`, 통화, 요청한 쪽 금액이 요청과 다르면 카드를 내지 않는다(`INVALID_RESPONSE`).

### fixture 견적 규칙(fake 모드·테스트 전용)

운영 견적은 Spring이 정한다. fixture는 아래처럼 계산한다.

- 보낼 KRW → 받을 금액: `floor(amount ÷ (rate ÷ unit))`. 500,000 KRW → 22,401 PHP
- 받을 금액 → 보낼 KRW: `ceil(amount × (rate ÷ unit))`. 20,000 PHP → 446,400 KRW

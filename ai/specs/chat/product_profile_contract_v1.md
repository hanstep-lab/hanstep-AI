# 챗봇 상품 추천 도구 ↔ Spring 내부 API 계약 v1 (product-profile)

챗봇 상품 추천 도구(`recommend_savings_products`, 이슈 #65)가 부르는 Spring 내부 API의 요청·응답 형식이다.
AI 쪽에서 정한 계약이며(2026-11-16 태건 승인), 구현은 BE가 한다. 구현하면서 바꿀 곳이 생기면 이 문서를 먼저 고친다.

- 인증: 다른 내부 API와 같이 `X-Internal-Token` 헤더, 요청 추적용 `X-Request-Id`.
- 조회만 한다. AI는 재시도하지 않는다(도구 시간 예산 `CHAT_TOOL_TIMEOUT_SECONDS`).
- 여유자금은 이 API에 없다. AI가 INT-06(`GET /internal/v1/ai-context/users/{userId}/finance`)의 `latestAnalysis.spare`를
  같은 때 함께 부른다.
- Spring 설정이 없거나 `CHAT_LLM_MODE=fake`이면 AI는 fixture 프로필을 쓴다(`tests/fixtures/chat/product_profiles.json`).

## 1. 상품 추천용 프로필

`GET /internal/v1/ai-context/users/{userId}/product-profile`

200:

```json
{"visaTypeCode": "E-9", "stayExpiresOn": "2027-10-04", "age": 30, "asOf": "2026-11-16"}
```

| 필드 | 형식 | 뜻 | 없을 때 |
|---|---|---|---|
| visaTypeCode | string(예: `E-9`, `E-7-4`) | 현재 비자 종류 코드(`visa_profiles` → `visa_types.code`) | 비자 프로필이 없으면 `null` |
| stayExpiresOn | date, ISO `YYYY-MM-DD` | 체류 만료일(`visa_profiles.stay_expires_on`) | 값이 없으면 `null` |
| age | int, 0 이상 | `asOf` 날짜(Asia/Seoul) 기준 만 나이. 생년월일(`visa_profiles.birth_date`)로 Spring이 계산한다 | 생년월일이 없으면 `null` |
| asOf | date, ISO `YYYY-MM-DD` | 계산 기준일 = Spring이 응답을 만든 날의 Asia/Seoul 날짜 | 항상 있음 |

- 생년월일 자체는 보내지 않는다(나이만).
- 사용자는 있지만 비자 프로필이 없으면 404가 아니라 200에 세 필드 모두 `null`이다.
- AI는 남은 체류 개월을 `asOf`와 `stayExpiresOn`으로 계산한다(추천기 `remaining_stay_months`와 같은 규칙).

오류(팀 공통 형식 `{code, message, details, requestId}`):

| 상태 | code | 뜻 |
|---|---|---|
| 401 | `UNAUTHORIZED` | 내부 토큰 없음·불일치 |
| 404 | `USER_NOT_FOUND` | 그 userId의 사용자가 없음 |

## 2. AI의 처리

| 상황 | 도구 결과 |
|---|---|
| 200 | 조건표 필터·기권 이유 계산. `null` 필드의 조건은 판정 불가로 두고(제외하지 않음) 답변에서 안내 |
| `stayExpiresOn` null | 체류 기간 조건 판정 불가, `STAY_TOO_SHORT` 이유는 내지 않음 |
| `age` null | 나이 조건 판정 불가 |
| INT-06 분석 결과 없음(`latestAnalysis` null) | 여유자금 조건·`REQUEST_EXCEEDS_SURPLUS` 판정 생략, 마이데이터 연결 안내 플래그 |
| 404 `USER_NOT_FOUND`, 그 밖의 오류 | 도구 `FAILED`, LLM에는 `data unavailable` |
| 시간 초과 | 도구 `TIMEOUT`, LLM에는 `data unavailable` |

## 3. 출처(`agent.sources`) — 제안, BE 확인 필요

추천한 상품마다 상품설명서 하나를 출처로 보낸다. 상품 문서 id는 조건표의 `productId`(문자열, 예: `doc-7b32d0e385aa`)라서
지금 `sources[].id`(숫자) 칸에 넣을 수 없다. 제안:

```json
{"type": "PRODUCT_DOCUMENT", "id": null, "chunkRef": "doc-7b32d0e385aa", "pageLabel": "p.1"}
```

- `chunkRef`에 문서 id 문자열, `pageLabel`에 근거 인용의 쪽(조건표 `source.page`)을 넣는다.
- Spring이 `PRODUCT_DOCUMENT`를 받아 저장·표시할 수 있어야 한다. 숫자 id가 필요하면 BE의 상품 문서 테이블 id로 바꾸고 이 절을 고친다.
- `agent.tool_call.resultRef`는 상품 추천에서 `null`이다(저장되는 분석 결과가 없음).

# 챗봇 환율 진단 도구 fx_rate_summary v1

챗봇이 "Is now a good time to send money home?" 같은 환율 질문에 답할 때 부르는 도구다(#91, `app/chat/fx_tools.py`).
Spring 내부 API(hanstep-backend#127)를 부르고, 그 값을 LLM에 줄 형식으로 바꾼다. 그래프 담당(이태건) 공유용이다.

- 숫자는 Spring이 계산한다(환율 화면 GET /api/v1/fx/rates와 같은 계산). LLM은 숫자를 옮겨 쓰고 설명만 한다.
- 과거 평균과의 비교만 준다. 예측·송금 권유·시점 추천은 없다.
- 환율은 실데이터만 쓴다(운영 `isMock`은 항상 false). 로컬 Mock 포함 설정에서는 `isMock: true`가 올 수 있다.
- 프롬프트 v4.4(`chat-v4.4` = v4.2 본문 그대로 + 환율 진단 규칙)에서만 붙는다. 다른 버전의 도구 구성은 그대로다. 기본 프롬프트(v4.1) 전환은 측정 결과를 보고 따로 정한다.

## 도구

| 항목 | 값 |
|---|---|
| 이름 | `fx_rate_summary` |
| 인자 | `currency`(선택, ISO 4217 대문자 세 글자. 소문자·앞뒤 공백은 고쳐 받는다). 빼면 사용자 화면 통화(USD + 모국 통화) 전부 |
| LLM 인자에 없는 것 | `userId`·요청 id(요청마다 고정. 넣으면 `INVALID_ARGUMENTS`로 거절) |
| Spring | `GET /internal/v1/ai-context/users/{userId}/fx-summary?currency=`(`X-Internal-Token`, `X-Request-Id`, 재시도 없음) |
| fake 모드·Spring 미설정 | `tests/fixtures/chat/fx_summary_reyes.json`(2026-11-18 로컬 Reyes 실제 출력) |

## LLM에 주는 결과(문장 없음)

```json
{
  "currencies": [
    {
      "currency": "PHP",
      "unit": 1,
      "quoteCurrency": "KRW",
      "rate": 20.984,
      "source": "MARKET_REF",
      "asOf": "2026-11-17",
      "lastCheckedAt": "2026-11-18T14:13:52+09:00",
      "changeFromPreviousBusinessDay": 0.112,
      "changePct": 0.54,
      "periodAverages": [
        {"period": "1M", "months": 1, "average": 21.0393, "difference": -0.0553, "differencePct": -0.26, "currentVsAverage": "LOWER", "businessDays": 22},
        {"period": "3M", "months": 3, "average": 21.5431, "difference": -0.5591, "differencePct": -2.6, "currentVsAverage": "LOWER", "businessDays": 67},
        {"period": "6M", "months": 6, "average": 22.9364, "difference": -1.9524, "differencePct": -8.51, "currentVsAverage": "LOWER", "businessDays": 132},
        {"period": "1Y", "months": 12, "average": 23.8157, "difference": -2.8317, "differencePct": -11.89, "currentVsAverage": "LOWER", "businessDays": 255}
      ],
      "insufficientDataPeriods": [],
      "isMock": false,
      "stale": false
    }
  ],
  "unavailableCurrencies": []
}
```

| 필드 | 뜻 |
|---|---|
| `rate` | `unit` 외화당 원화. 일별 고시 환율(KOREAEXIM 매매기준율 또는 ECB 참고값) |
| `source` | `KOREAEXIM`(수출입은행 고시) / `MARKET_REF`(ECB 기준환율 기반 참고값, 송금 환율 아님) / `MOCK`(로컬) |
| `asOf` · `lastCheckedAt` | 기준일 · 원천에서 마지막으로 확인한 시각(+09:00) |
| `periodAverages[]` | 기준일부터 1·3·6·12개월(시작일 포함) 영업일 값 평균. `difference`=현재−평균, `differencePct`=그 차이 ÷ 평균 × 100(소수 2자리). `currentVsAverage`는 현재 값이 평균보다 `LOWER`·`HIGHER`, ±0.10% 이내면 `SAME` |
| `insufficientDataPeriods` | 데이터가 기간보다 짧아 평균을 내지 않은 기간 키(평균 숫자는 주지 않음) |
| `unavailableCurrencies` | 실데이터가 없어 뺀 화면 통화(currency를 뺐을 때만) |
| `isMock` · `stale` | Mock 환율인지 · 값이 오래됐는지(KOREAEXIM은 기준일이 직전 평일보다 이름, MARKET_REF는 마지막 확인이 3시간 넘음) |

결과 참조(resultRef)·근거(sources)는 없다. 화면 카드는 아래 "화면 카드" 절(#95). 측정용으로 같은 값을 `agent.metrics`의 `tools[].content`에 남긴다.

## 오류

| Spring | 도구 상태 | LLM이 받는 내용 |
|---|---|---|
| 404 `FX_RATE_NOT_FOUND`(그 통화 실데이터 없음, 생략 때는 화면 통화 전부 없음) | `FAILED` | `no exchange rate data for this currency` |
| 400 `VALIDATION_ERROR`(화면 통화가 아님) | `REJECTED` | `this currency is not one of the user's currencies; call again without currency to get all of the user's currencies` |
| 시간 초과 | `TIMEOUT` | `data unavailable` |
| 그 밖(404 `USER_NOT_FOUND`, 401, 5xx, 연결 실패, 본문 형식 오류) | `FAILED` | `data unavailable` |

## 화면 카드(chat.done blocks, `FX_SUMMARY_CARD`, #95)

도구가 `SUCCEEDED`이면 AI는 `agent.done`의 `blocks`에 환율 진단 카드를 넣는다. Spring은 이 값을 `chat_messages.blocks_json`에 저장하고 `chat.done`으로 그대로 보낸다(대화 기록 재조회도 같은 값). 화면은 피그마 AI-004 안의 카드(277:1852)다.

- 숫자는 코드가 Spring 원본(fx-summary 응답)에서만 채운다. LLM이 쓴 숫자는 넣지 않는다. LLM에 주는 결과(위 절)는 카드와 상관없이 그대로다.
- 카드는 LLM용 결과가 아니라 원본으로 만든다. 그래서 LLM에 주지 않는 송금 환율(`sendRate`·`receiveRate`)이 있다.
- 필드 이름은 환율 화면 API(FX-001 `GET /api/v1/fx/rates`, `FxRateResponse`)와 같다. 그중 `sourceLabel`·`notices`·`rateAt`·`screenCurrencies`는 넣지 않는다(표시 문구는 앱이 `source`로 정한다).
- 문장(요약·권유·예측)과 버튼 경로는 넣지 않는다.
- `FAILED`·`REJECTED`·`TIMEOUT`이면 카드가 없다. 통화가 하나도 없으면 카드가 없다. 카드 값만 형식이 어긋나면(예: 환율 0) 답변은 그대로 두고 카드만 내지 않는다.

### 버튼은 앱이 고정으로 붙인다(`detailPath` 없음)

`환율 상세 보기`·`이 환율로 바로 송금하기`·`환율 알림 받기`는 카드 값과 상관없이 늘 같은 화면으로 가므로 앱이 고정으로 붙인다. 그래서 이슈 초안의 `detailPath`("/fx")는 블록에 넣지 않았다. 송금 확인 카드의 `transferPath`는 받는 분·금액이 카드마다 달라 블록에 있다. 피그마의 `일본 JPY 환율 보기`는 수집하지 않는 통화라 범위 밖이다.

### 한 턴에 카드 하나(통화 기준 병합)

카드는 하나에 통화 목록(USD + 모국 통화)을 담는다. 한 턴에 `fx_rate_summary`를 여러 번 불러도(예: `currency=USD`, `currency=PHP`) 스트림이 하나로 합친다.

- 카드 자리는 처음 나온 FX 카드 자리, 통화 순서는 처음 나온 순서.
- 같은 통화는 나중 호출 값으로 덮어쓴다.
- `unavailableCurrencies`는 모든 호출 것을 합친 뒤 카드에 실제로 있는 통화를 뺀다.
- 실패한 호출은 카드에 아무것도 더하지 않는다(USD 성공 + PHP 실패면 USD만).

### 필드

| 필드 | 뜻 | null |
|---|---|---|
| `type` | `"FX_SUMMARY_CARD"` | |
| `currencies[]` | 통화 목록(1개 이상) | |
| `currencies[].currency` · `unit` | 통화(ISO 4217) · 고시 단위(VND 등은 100). `unit` 외화 = `rate` 원 | |
| `rate` | 매매기준율(일별 고시값) | |
| `sendRate` · `receiveRate` | 송금 보낼 때 · 받을 때 환율 | `KOREAEXIM`(USD)만 있고 `MARKET_REF`(PHP 등)는 null |
| `change` · `changeRate` | 전 영업일 대비(원) · (%) | 전 영업일 값이 없으면 null |
| `source` | `KOREAEXIM`(수출입은행 고시) / `MARKET_REF`(ECB 기반 참고값, 송금 환율 아님) / `MOCK`(로컬) | |
| `asOf` · `fetchedAt` | 기준일 · 원천에서 마지막으로 확인한 시각(+09:00) | |
| `periodAverages[]` | 평균을 낸 기간만(1M·3M·6M·1Y 순). 데이터가 부족한 기간은 항목 자체가 없다(빈 배열일 수 있음) | |
| `periodAverages[].avg` · `diff` · `diffRate` | 평균 · 현재−평균 · 그 차이 ÷ 평균 × 100(%) | 없음(부족 기간은 빠지므로) |
| `periodAverages[].direction` · `businessDays` | 현재가 평균보다 `LOWER`·`HIGHER`, ±0.10% 이내면 `SAME` · 평균에 쓴 영업일 수 | |
| `isMock` · `stale` | Mock 환율인지(운영은 항상 false) · 값이 오래됐는지 | |
| `unavailableCurrencies` | 실데이터가 없어 카드에 없는 화면 통화(currency를 뺀 호출에서만 생긴다) | 없으면 `[]` |

피그마에는 기간 평균이 없다. `periodAverages`를 보여 줄지·어떻게 보여 줄지는 앱이 정한다.

### 실제 `agent.done` blocks 샘플

fake 모델이 `fx_rate_summary`(currency 생략)를 부르고 fixture(`tests/fixtures/chat/fx_summary_reyes.json`, 2026-11-18 로컬 Reyes 실제 출력)를 받은 실행의 출력이다. USD는 로컬 Mock 시드라 `isMock`·`stale`이 true다.

```json
[
  {
    "type": "FX_SUMMARY_CARD",
    "currencies": [
      {
        "currency": "USD",
        "unit": 1,
        "rate": 1406.51,
        "sendRate": 1420.5751,
        "receiveRate": 1392.4449,
        "change": -1.08,
        "changeRate": -0.08,
        "source": "KOREAEXIM",
        "asOf": "2026-11-06",
        "fetchedAt": "2026-11-06T11:05:00+09:00",
        "periodAverages": [
          {"period": "1M", "avg": 1407.7735, "diff": -1.2635, "diffRate": -0.09, "direction": "SAME", "businessDays": 23},
          {"period": "3M", "avg": 1429.6566, "diff": -23.1466, "diffRate": -1.62, "direction": "LOWER", "businessDays": 62},
          {"period": "6M", "avg": 1419.9966, "diff": -13.4866, "diffRate": -0.95, "direction": "LOWER", "businessDays": 126},
          {"period": "1Y", "avg": 1418.8393, "diff": -12.3293, "diffRate": -0.87, "direction": "LOWER", "businessDays": 246}
        ],
        "isMock": true,
        "stale": true
      },
      {
        "currency": "PHP",
        "unit": 1,
        "rate": 20.984,
        "sendRate": null,
        "receiveRate": null,
        "change": 0.112,
        "changeRate": 0.54,
        "source": "MARKET_REF",
        "asOf": "2026-11-17",
        "fetchedAt": "2026-11-18T14:13:52+09:00",
        "periodAverages": [
          {"period": "1M", "avg": 21.0393, "diff": -0.0553, "diffRate": -0.26, "direction": "LOWER", "businessDays": 22},
          {"period": "3M", "avg": 21.5431, "diff": -0.5591, "diffRate": -2.6, "direction": "LOWER", "businessDays": 67},
          {"period": "6M", "avg": 22.9364, "diff": -1.9524, "diffRate": -8.51, "direction": "LOWER", "businessDays": 132},
          {"period": "1Y", "avg": 23.8157, "diff": -2.8317, "diffRate": -11.89, "direction": "LOWER", "businessDays": 255}
        ],
        "isMock": false,
        "stale": false
      }
    ],
    "unavailableCurrencies": []
  }
]
```

### 측정 영향

LLM에 주는 결과와 측정 값(`agent.metrics`)은 그대로다. 측정 스크립트(`scripts/evaluate_chat.py`)는 송금 문항에서 `blocks`를 숫자 근거로 쓰는데, 이 카드는 근거에서 뺀다(LLM이 받지 않은 `sendRate` 등이 들어가지 않게). 환율 진단 숫자 근거는 LLM에 준 값(`fx_results`) 그대로다.

## 프롬프트 v4.4에 더한 규칙(`app/chat/prompts/fx_rules.md`)

- 환율 질문(현재 값, 지난달·과거와 비교, 지금 보내도 되는지)은 `fx_rate_summary`를 먼저 부른다. 이런 질문에 `get_fx_rate`는 쓰지 않는다.
- 도구 결과의 숫자만 그대로 쓴다(계산·반올림·환산 금지).
- 과거 평균과의 비교만 한다. 오를지 내릴지, 지금이 좋은 때인지 말하지 않고, 지금 보내라·기다려라 하지 않는다. 예측이나 시점을 물으면 예측할 수 없다고 말하고 과거 비교를 준다.
- 환율은 `<unit> <currency> = <rate> KRW`로 쓴다.
- 근거를 함께 말한다: 원천, 기준일(`asOf`), 말한 평균마다 영업일 수.
- `MARKET_REF`는 "market reference rate"(ECB 기반, 송금 환율 아님), `KOREAEXIM`은 "Korea Eximbank published rate"로 부른다.
- `isMock`이면 데모 데이터라고 분명히 말한다.
- `stale`이면 기준일과 마지막 확인 시각을 강조하고 값이 오래됐을 수 있다고 말한다.
- `insufficientDataPeriods`의 기간은 데이터가 부족하다고 말하고 평균을 짐작하지 않는다.
- 도구가 `data unavailable`·`no exchange rate data`를 주면 지금 환율을 확인할 수 없다고 말하고 환율을 말하지 않는다.
- 굵게 등 마크다운 강조를 쓰지 않는다.

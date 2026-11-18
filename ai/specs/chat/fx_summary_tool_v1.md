# 챗봇 환율 진단 도구 fx_rate_summary v1

챗봇이 "Is now a good time to send money home?" 같은 환율 질문에 답할 때 부르는 도구다(#91, `app/chat/fx_tools.py`).
Spring 내부 API(hanstep-backend#127)를 부르고, 그 값을 LLM에 줄 형식으로 바꾼다. 그래프 담당(이태건) 공유용이다.

- 숫자는 Spring이 계산한다(환율 화면 GET /api/v1/fx/rates와 같은 계산). LLM은 숫자를 옮겨 쓰고 설명만 한다.
- 과거 평균과의 비교만 준다. 예측·송금 권유·시점 추천은 없다.
- 환율은 실데이터만 쓴다(운영 `isMock`은 항상 false). 로컬 Mock 포함 설정에서는 `isMock: true`가 올 수 있다.
- 프롬프트 v5(`chat-v5`)에서만 붙는다. 기본 프롬프트는 아직 v4.1이다.

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

화면 카드(blocks)·결과 참조(resultRef)·근거(sources)는 없다. 측정용으로 같은 값을 `agent.metrics`의 `tools[].content`에 남긴다.

## 오류

| Spring | 도구 상태 | LLM이 받는 내용 |
|---|---|---|
| 404 `FX_RATE_NOT_FOUND`(그 통화 실데이터 없음, 생략 때는 화면 통화 전부 없음) | `FAILED` | `no exchange rate data for this currency` |
| 400 `VALIDATION_ERROR`(화면 통화가 아님) | `REJECTED` | `this currency is not one of the user's currencies; call again without currency to get all of the user's currencies` |
| 시간 초과 | `TIMEOUT` | `data unavailable` |
| 그 밖(404 `USER_NOT_FOUND`, 401, 5xx, 연결 실패, 본문 형식 오류) | `FAILED` | `data unavailable` |

## 프롬프트 v5에 더한 규칙(`app/chat/prompts/fx_rules.md`)

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

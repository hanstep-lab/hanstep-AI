# 챗봇 v4.5 측정 결과: 입력값 기준 월 수지·목표 안내·금융상품 추천 카드 (#114, 2026-11-20)

설계: `specs/chat/chat_v45_measure_v1.md`(측정 전 커밋 `4afdab2`·`2334714`·`5ccbff2`). 카드 계약: `specs/chat/product_recommend_card_v1.md`.
구현: `4945ae7`. develop 병합: `937022e`(#111 포함).

## 0. 판단

- **v4.5 전환 가능.** 입력값 기준 답변·출처 언급·목표 반영·카드는 v4.5에서 판정 기준을 넘었다. 판단형 10문항 회귀는 없다.
- 적용 범위
  - **숫자 기준 변경(INT-06 `balance` 사용, 상품 도구 `balance.spare`)은 모든 버전**(v4.4 포함)에 적용된다. 이 브랜치가 머지되면 운영 v4.4도 바로 입력값 기준 숫자를 받는다.
  - **상품 추천 카드와 규칙(출처 언급, 음수 여유자금 표현, 목표 순서 안내, 메뉴 이름, 카드가 붙을 때 금리는 카드를 가리킴)은 v4.5만**이다.
- 남은 점
  - C2(SETTLEMENT → LIVING_COST)의 목표 **순서** 일치는 0/6이다. v4.5는 두 목표를 모두 말하지만, 여유자금이 음수라 변동지출을 먼저 다룬다(3절).
  - 근거 없는 숫자 증가는 채점기 한계(음수 여유자금의 절댓값 600,000)로 생긴 오판이다. 그 밖의 근거 없는 숫자는 0이다(3절).
  - **v4.4(새 코드)는 입력값 숫자를 "past averages"라고 잘못 부른다**(출처 언급 0/8). 머지 뒤 운영이 v4.4로 남아 있으면 이 표현이 나온다. v4.5 전환을 같이 하는 것을 권한다.

## 1. 측정 조건

| 항목 | 값 |
|---|---|
| 날짜 | 2026-11-20(KST). 기준선 13시대, after 16시대 |
| 모델 | `claude-haiku-5-5`, thinking model_default, maxTokens 2000, judgmentFacts true |
| 설정 | 기준선 `variants_v45_baseline_v1.json`(v4.4), after `variants_v45_v1.json`(v4.4·v4.5, `X-Chat-Config`, 서버 `CHAT_ALLOW_CONFIG_OVERRIDE=true`) |
| AI | 이 브랜치를 로컬 8001로 띄움. 기준선은 `5ccbff2`(측정 파일만, 옛 코드), after는 `4945ae7`(새 코드) |
| Spring | 로컬 8082, hanstep-backend `feat/151-unified-balance` = develop #153과 같은 코드(INT-06 `balance` 있음). AI 작업 재발행·환율 수집 끔 |
| DB | 로컬 MySQL 3307(BE 세션과 함께 씀). 운영 접속 없음 |
| 사용자 | Reyes(userId 1). 확보액 USER: 고정 1,250,000 · 송금 700,000 · 변동 1,000,000. INT-06 `balance.spare` −600,000(거래 기준 `latestAnalysis.spare`는 675,317) |
| 목표 조합 | C1 LUMP_SUM(6,000,000원, 2027-09-30, monthlyRequired 600,000) → CREDIT, C2 SETTLEMENT → LIVING_COST. 측정할 때만 바꾸고 바로 되돌림 |
| 문항 | `v45_questions_v1.json` 6문항 × 조합 2 × 2회 = 설정당 24회. 판단형 회귀 eval J01~J10 × 1회 × 2설정(목표 LUMP_SUM만) |
| 외부 API | 수출입은행 등 한도 있는 API 호출 없음 |
| 앱 경로 | **앱 경로 확인은 운영에서 한다**: 머지 뒤 운영을 v4.5로 바꾸고 상품 질문 1개로 카드·대화 기록 재조회를 확인한다(로컬 스모크는 2026-11-20 결정으로 뺌) |

| run | 회차 | 오류 | 비용(USD) |
|---|---|---|---|
| 기준선 C1·C2 | 24 | 0 | 0.0514 |
| after C1·C2(v4.4 새 코드·v4.5) | 48 | 0 | 0.1034 |
| 판단형 회귀(v4.4 새 코드·v4.5) | 20 | 0 | 0.0439 |
| 합계 | 92 | 0 | 0.1987 |

- 측정 뒤 로컬 DB는 시드 값으로 되돌렸다(확보액 월세 400,000 · 공과금 70,000 · 전기·수도 50,000 · 송금 700,000 · 변동 450,000, 목표 LUMP_SUM만). 띄운 AI 서버는 종료했다.

## 2. 결과

- 기준선은 출처 패턴에 "declared"를 더한 규칙으로 저장 결과만 다시 채점했다(2026-11-20 결정, LLM 재호출 없음, `*_rescored.jsonl`).
- 비교 방법: 기준선 → v4.4(새 코드)의 차이는 **숫자 기준 변경(코드) 효과**다. v4.4(새 코드) → v4.5의 차이는 **규칙·카드(프롬프트) 효과**다.

| 지표 | 기준선 v4.4(옛 코드) | v4.4(새 코드) | v4.5 |
|---|---|---|---|
| **V01 여유자금이 입력값 기준(−600,000)** (주 지표) | 0/4 | 4/4 | 4/4 |
| V02 세 항목이 입력값과 일치 | 3/4 | 4/4 | 4/4 |
| 입력값 질문 출처 언급 | 4/8 | 0/8 | 8/8 |
| 입력값 질문 거래 기준 숫자 사용 | 8/8 | 0/8 | 0/8 |
| 열린 질문 C1 고른 목표 모두 언급 | 2/6 | 0/6 | 6/6 |
| 열린 질문 C1 순서 일치 | 2/6 | 0/6 | 6/6 |
| 열린 질문 C2 고른 목표 모두 언급 | 5/6 | 2/6 | 6/6 |
| 열린 질문 C2 순서 일치 | 2/6 | 0/6 | 0/6 |
| V06 상품 도구 여유자금 | 675,317 | −600,000 | −600,000 |
| V06 추천 수 | 3 | 1 | 1 |
| V06 카드 있음 · 카드=도구 결과 | 0/4 · - | 0/4 · - | 4/4 · 4/4 |
| V06 금리 숫자 직접 언급 | 0/4 | 0/4 | 0/4 |
| V06 카드 가리킴 | 0/4 | 0/4 | 4/4 |
| 근거 없는 숫자 없음(채점기 그대로) | 24/24 | 13/24 | 12/24 |
| 근거 없는 숫자 없음(600,000 오판 제외, 3절) | 24/24 | 24/24 | 24/24 |
| 도구 일치 | 24/24 | 24/24 | 24/24 |

- 기준선의 V02 "일치"와 "출처 언급"은 입력값을 기준으로 답한 것이 아니다. 거래 기준으로 답한 뒤 "The amounts you declared during onboarding are different (1,250,000 …)"처럼 차이를 지적한 문장이다(부록).
- V06: 여유자금이 −600,000이 되자 월 납입 범위 근거가 없는 상품 1개(한 달부터 적금, `notChecked`에 spareMoney)만 통과했다. v4.5 답변은 "Your monthly spare money shows as -600000 KRW, which means the spending you entered is more than your income"이라고 함께 말했다.
- 열린 질문에서 v4.5가 묻지 않은 도구(E-7-4·송금·환율)를 부른 회차는 0이다(모두 `get_financial_analysis`만).

### 2-1. 판단형 회귀 (J01~J10 × 1회, 같은 DB)

| 설정 | 도구 일치 | 근거 없는 숫자 없음 | 오류 |
|---|---|---|---|
| v4.4(새 코드) | 10/10 | 10/10 | 0 |
| v4.5 | 10/10 | 10/10 | 0 |

- 2026-11-20 결정대로 #101 저장 결과와 직접 비교하지 않고 같은 DB에서 두 설정을 함께 쟀다.

## 3. 해석

- **숫자 기준 변경(코드):** v4.4도 새 코드에서는 V01·V02가 입력값 숫자로 답했다(0/4 → 4/4). 하지만 출처는 말하지 않고, 일부는 "This is based on your past averages"라고 잘못 말한다.
- **규칙 효과(v4.5):**
  - 출처 언급 8/8("Based on the amounts you entered")
  - 음수 여유자금은 "your monthly spending is more than your monthly income"과 함께 도구 값 그대로(−600,000) 말함. 새 계산은 없음
  - C1 목표 순서 6/6. 예: CREDIT은 '대안신용 사전진단' (alternative credit check) 메뉴로 안내
  - 카드 4/4, 카드 가리킴 4/4, 금리 숫자 0
- **C2 순서 0/6:** v4.5는 SETTLEMENT('비자 로드맵' 안내)와 LIVING_COST를 모두 말하지만, 음수 여유자금 안내에서 변동지출(LIVING_COST 낱말)이 먼저 나온다.
  - 규칙 두 개(음수 여유자금 안내, 목표 순서)가 겹친 결과로 본다.
  - 낱말 위치로 보는 채점 방식도 이 경우를 구분하지 못한다(4절 ③). 운영 전환을 막을 정도는 아니라고 본다. 순서를 더 엄격히 맞출지는 후속으로 둔다.
- **근거 없는 숫자 오판:** v4.4(새 코드)·v4.5의 근거 없는 숫자는 모두 C2의 "600,000"(−600,000의 절댓값, "short by 600,000 KRW" 등)이다.
  - 숫자 검사는 음수의 절댓값을 환율 도구 결과에서만 근거로 인정한다(#94). 그래서 C2에서 오판이 생긴다.
  - C1은 LUMP_SUM의 monthlyRequired가 마침 600,000이라 근거로 잡혔다.
  - 측정 뒤라 채점기는 고치지 않았다. **제안:** 금융 분석 결과의 음수 값(여유자금)도 절댓값을 근거로 인정한다.

## 4. 한계

1. **입력값이 있으면 LLM은 거래 기준 숫자를 받지 않는다.**
   - `analysis`의 금액이 balance(입력값 우선) 값으로 바뀌기 때문이다.
   - 그래서 '거래 기준 숫자 사용' 지표는 after에서 뜻이 바뀐다. 거래 기준 값이 도구 결과에 없으므로, 나오면 근거 없는 숫자다.
   - 사용자가 "실제 거래로는 얼마 썼나"를 물으면 지금은 답할 수 없다.
2. **`test_finance_properties`가 전체 단위 테스트 실행 중 1회 실패했다.**
   - 단독 실행은 6/6 통과다. 원인은 확인하지 않았다(이 브랜치가 고치지 않은 금융 계산 속성 테스트).
   - develop 병합 뒤 전체 실행(1914 통과)에서는 재현되지 않았다.
3. **판정 방식 한계**
   - ① 카드 가리킴은 답변에 "card" 단어가 있는지만 본다.
   - ② 출처 패턴은 "you entered"·"you declared"류만 잡는다. "declared"는 차이를 지적한 문장에도 걸린다(기준선 V02).
   - ③ 목표 언급은 낱말 위치로 본다. 목표 이름만 나열해도 언급으로 세고(예: "your settlement and living cost goals"), 다른 맥락의 낱말(음수 여유자금 안내의 "variable spending")도 순서에 들어간다.

## 5. 운영 전환

- 전환: 서버 `deploy/.env`에 `CHAT_PROMPT_VERSION=v4.5`를 넣고 ai-server 컨테이너를 다시 만든다.
- 되돌리기: `CHAT_PROMPT_VERSION=v4.4`로 바꾸고 ai-server를 다시 만든다. 숫자 기준 변경(balance 사용)은 v4.4에도 남는다(1절 적용 범위).
- 확인(운영): 상품 질문 1개로 `PRODUCT_RECOMMEND_CARD`가 `chat.done.blocks`에 오는지, 대화 기록 재조회(CHT-03)에서 같은 카드가 오는지 본다.

## 6. 후속

- #106 언어 검사기는 따옴표 메뉴 이름('대안신용 사전진단' 등)을 병기로 인정하지 않는다. 언어를 다시 잴 때 허용 목록에 넣을지 정한다.
- 숫자 검사가 금융 분석의 음수 값 절댓값을 근거로 인정하게 한다(3절 제안).
- C2처럼 음수 여유자금과 목표 순서가 겹칠 때 어떤 순서로 안내할지 정한다.
- `chat_blocks_v1.md`에 `PRODUCT_RECOMMEND_CARD`를 더한다(#108 머지 뒤로 미룬 것, 이제 가능).

## 부록. V01·V02 답변 원문(앞 두 문장, 사람 확인용)

결과 파일: `artifacts/chat/v45_baseline_c{1,2}_rescored.jsonl`, `v45_after_c{1,2}.jsonl`(커밋하지 않음).

### 기준선 v4.4(옛 코드)

- C1 V01#1 (일치 X, 출처 X): About 675,317 KRW is left each month after your usual living costs and your usual remittance home. This is based on an average monthly income of 2,350,000 KRW, fixed living costs of 520,000 KRW, a remittance of 700,000 KRW, and variable spending that averages 454,683 KRW.
- C1 V01#2 (일치 X, 출처 X): About 675,317 KRW is left each month on average after your fixed living costs, your usual remittance of 700,000 KRW, and your variable spending. That is based on your average monthly income of 2,350,000 KRW, fixed living costs of 520,000 KRW, and average variable spending of 454,683 KRW, which ranged from 410,600 to 512,400 KRW.
- C1 V02#1 (일치 O, 출처 O): On average over the past 12 months, your fixed living costs are 520,000 KRW a month. You send about 700,000 KRW home each month.
- C1 V02#2 (일치 X, 출처 O): Over the past 12 months, your average monthly fixed living costs were 520,000 KRW, your usual remittance was 700,000 KRW, and your other spending averaged 454,683 KRW (monthly range 410,600 to 512,400 KRW). That leaves about 675,317 KRW a month as spare money after sending the usual remittance.
- C2 V01#1 (일치 X, 출처 X): About 675,317 KRW is left each month after your usual living costs, variable spending and the usual remittance home. That is about 28.7% of your average monthly income of 2,350,000 KRW.
- C2 V01#2 (일치 X, 출처 X): On average, about 675,317 KRW is left each month after your usual expenses and the usual remittance home, out of average monthly income of 2,350,000 KRW. That figure already subtracts fixed living costs of 520,000 KRW, the usual remittance of 700,000 KRW, and average variable spending of 454,683 KRW.
- C2 V02#1 (일치 O, 출처 O): Over the 12 months from 2025-11-01 to 2026-10-31, your fixed living costs averaged 520,000 KRW a month. You sent home about 700,000 KRW a month, and your other spending averaged 454,683 KRW, ranging from 410,600 to 512,400 KRW.
- C2 V02#2 (일치 O, 출처 O): On average over the past 12 months, your fixed living costs are 520,000 KRW a month, and you send about 700,000 KRW home. Other spending averages 454,683 KRW a month, ranging from 410,600 to 512,400 KRW.

### v4.4(새 코드)

- C1 V01#1 (일치 O, 출처 X): On average, you have no money left each month. Your spending is about 600,000 KRW more than your income.
- C1 V01#2 (일치 O, 출처 X): On average, you do not have money left each month. Your spending is about 600,000 KRW more than your income.
- C1 V02#1 (일치 O, 출처 X): On average, your fixed living costs are 1,250,000 KRW a month, your remittance to family is 700,000 KRW, and other spending is about 1,000,000 KRW. Your average monthly income is 2,350,000 KRW, so after these costs your spare money comes to -600,000 KRW a month.
- C1 V02#2 (일치 O, 출처 X): Your average monthly fixed living costs are 1,250,000 KRW, remittance to family is 700,000 KRW, and other spending is about 1,000,000 KRW. Together these come to more than your average monthly income of 2,350,000 KRW, leaving a monthly shortfall of 600,000 KRW.
- C2 V01#1 (일치 O, 출처 X): After your usual expenses and the usual remittance home, you have no money left each month. On average you spend about 600,000 KRW more than you earn.
- C2 V01#2 (일치 O, 출처 X): On average, you have no money left each month. Your spare money is -600,000 KRW, so your expenses are about 600,000 KRW more than your income.
- C2 V02#1 (일치 O, 출처 X): Your average monthly spending over the past 12 months (November 2025 to October 2026) is 1,250,000 KRW on fixed living costs, 700,000 KRW on sending money home, and 1,000,000 KRW on other variable spending. Together these are more than your average monthly income of 2,350,000 KRW, leaving a spare amount of -600,000 KRW each mont…
- C2 V02#2 (일치 O, 출처 X): Your average monthly figures from November 2025 to October 2026 are 1,250,000 KRW for fixed living costs, 700,000 KRW for remittance, and 1,000,000 KRW for other spending. Together these are more than your average monthly income of 2,350,000 KRW, which leaves a spare amount of -600,000 KRW each month.

### v4.5

- C1 V01#1 (일치 O, 출처 O): Based on the amounts you entered, you have no money left over each month. Your spare amount is -600,000 KRW, which means your monthly spending is more than your monthly income of 2,350,000 KRW.
- C1 V01#2 (일치 O, 출처 O): You have no money left over each month. Based on the amounts you entered, your monthly spending is more than your monthly income, so your spare money is -600000 KRW.
- C1 V02#1 (일치 O, 출처 O): Based on the amounts you entered, you spend 1,250,000 KRW a month on fixed living costs, 700,000 KRW on remittances home, and 1,000,000 KRW on other spending. Together that is 2,950,000 KRW, which is more than your average monthly income of 2,350,000 KRW, so your monthly spare money is -600,000 KRW.
- C1 V02#2 (일치 O, 출처 O): Based on the amounts you entered, you spend 1,250,000 KRW a month on fixed living costs, 700,000 KRW on sending money home, and 1,000,000 KRW on other spending. Together that is 2,950,000 KRW, while your average monthly income is 2,350,000 KRW.
- C2 V01#1 (일치 O, 출처 O): Based on the amounts you entered, your monthly spending is more than your monthly income, so nothing is left over. Your spare money comes out to -600000 KRW per month.
- C2 V01#2 (일치 O, 출처 O): You have no money left over each month. Based on the amounts you entered, your monthly spending is more than your monthly income.
- C2 V02#1 (일치 O, 출처 O): Based on the amounts you entered, your monthly spending is 1,250,000 KRW for fixed living costs, 700,000 KRW for sending money home, and 1,000,000 KRW for other spending. Your average monthly income is 2,350,000 KRW, so the monthly spending you entered is more than your income, and your spare money is -600,000 KRW.
- C2 V02#2 (일치 O, 출처 O): Based on the amounts you entered, you spend about KRW 1,250,000 a month on fixed living costs, KRW 700,000 on sending money home, and KRW 1,000,000 on other spending. Your average monthly income is KRW 2,350,000, so your monthly spending is more than your income, and your spare money is KRW -600,000 a month.
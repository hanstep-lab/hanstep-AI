# 챗봇 v4.5 측정 설계: 입력값 기준 여유자금·목표 반영 (#114, 2026-11-20)

측정 전에 고정하는 문서다. 기준선(v4.4)을 재기 전에 커밋하고, 측정 뒤에는 채점기(`scripts/_v45_check.py`)를 고치지 않는다(필요하면 제안만 한다).
카드 계약은 `specs/chat/product_recommend_card_v1.md`다.

## 1. 무엇을 보나

- (a) 입력값 기준: 사용자가 "매달 나가는 돈"에 직접 입력한 값이 있으면, 답변이 그 값으로 말하고 "입력하신 값 기준"이라고 밝히는지
- (b) 목표 반영: 열린 조언 질문에서 금융 도구로 목표를 확인하고, 사용자가 고른 목표를 고른 순서대로 다루는지
- 근거 없는 숫자 0 유지(기존 숫자 검사)

## 2. 문항 (`specs/chat/v45_questions_v1.json`, 5문항)

| id | 분류 | 질문 | 채점 |
|---|---|---|---|
| V01 | 입력값 | How much money do I have left each month after my expenses? | 여유자금(`v45Expect: spare`) |
| V02 | 입력값 | How much do I spend each month on fixed living costs, sending money home and other spending? | 세 항목(`v45Expect: items`) |
| V03 | 열린 질문 | What should I focus on with my money right now? | 고른 목표 |
| V04 | 열린 질문 | Can you give me some financial advice? | 고른 목표 |
| V05 | 열린 질문 | What should I do next to get closer to my goals? | 고른 목표 |

- 기대 도구는 모두 `get_financial_analysis`다(v4.4·v4.5 같음). 열린 질문에서 v4.5가 다른 도구(예: E-7-4)를 더 부르는 것은 도구 일치 판정에 쓰지 않고 따로 적는다.
- 회차: 문항 5 × 목표 조합 2 × 2회 = 20회(설정 하나). after도 같다.

## 3. 채점 기준 (`scripts/_v45_check.py`)

기대값은 문항에 적지 않는다. 측정 때 스크립트가 읽은 INT-06 요약(행의 `toolResult`)에서 정한다.

### 3-1. 입력값 질문 (V01·V02)

- 입력값: `declaredCommitments`의 `fixedLiving`·`remittance`·`variableExpense`다. 입력이 없거나 0인 항목은 거래 기준(`analysis`) 값을 쓴다(BE #151 `MonthlyBalance`와 같은 규칙).
- 입력값 기준 여유자금 = `analysis.avgMonthlyIncome` − 위 세 항목이다(채점용 계산).
- 지표
  - **입력값 숫자 일치**(`inputMatch`): V01은 여유자금, V02는 세 항목 모두가 답변에 있다.
    - 반올림(1,000·10,000원)도 일치로 본다.
    - 음수 여유자금은 절댓값으로 본다("600,000 won short").
  - **거래 기준 숫자 사용**(`usedTransactionNumbers`): 입력값과 다른 거래 기준 값이 답변에 있다.
  - **출처 언급**(`sourceMentioned`): "you entered", "based on the amounts you entered" 같은 표현이 있다.
  - 근거 없는 숫자 없음: 기존 `numberCheck.unsupported`가 비었다.

### 3-2. 열린 질문 (V03~V05)

- 고른 목표: `goals[].goalType` 순서다(INT-06은 ACTIVE 목표를 id 순서로 준다).
- 목표별 낱말
  - LUMP_SUM: lump sum, save up, savings goal, target amount, 6,000,000
  - CREDIT: credit
  - SETTLEMENT: E-7-4, long-term stay, settle, visa roadmap
  - LIVING_COST: living costs, variable spending, budget
- 지표
  - **언급 비율**: 언급한 고른 목표 수 ÷ 고른 목표 수
  - **모두 언급**: 비율이 1인 회차 수
  - **순서 일치**: 모두 언급했고, 첫 언급 위치가 고른 순서와 같은 회차 수
  - 근거 없는 숫자 없음

### 3-3. after 판정 기준 (v4.5)

- 입력값 숫자 일치: V01·V02 전 회차
- 출처 언급: 입력값 질문 전 회차
- 고른 목표 모두 언급: 열린 질문 전 회차의 80% 이상
- 근거 없는 숫자: 기준선보다 늘지 않음
- 회귀: 판단형 10문항(eval J01~J10) × 1회. 도구 일치·숫자 검사가 v4.4와 같거나 낫다(같은 DB에서 v4.4도 같이 돈다).

## 4. 목표 조합과 로컬 DB 상태

- 사용자: 로컬 Reyes(userId 1), MySQL 3307(BE 세션과 함께 씀). 운영 DB에는 접속하지 않는다.
- 측정 전 상태(2026-11-20 읽기만 함)

  | 표 | 값 |
  |---|---|
  | `monthly_commitments`(is_active=1) | LIVING_MINIMUM 1,250,000 · REMITTANCE 700,000 · VARIABLE_EXPENSE 1,000,000(모두 USER) |
  | `financial_analyses`(최신 id 1) | 월소득 2,350,000 · 고정 520,000 · 송금 700,000 · 변동 454,683 · 여유 675,317(12개월) |
  | `financial_goals`(ACTIVE) | id 1 LUMP_SUM 6,000,000원, 2027-09-30 |
  | `onboarding_profiles` | 행 없음 |

  - 입력값 기준 여유자금은 2,350,000 − 1,250,000 − 700,000 − 1,000,000 = **−600,000**이다(거래 기준 675,317과 다름).
- 목표 조합(측정할 때만 바꾸고, 바꾸기 전에 사용자에게 먼저 알린다)
  - C1: LUMP_SUM(지금 행) → CREDIT
  - C2: SETTLEMENT → LIVING_COST
- 확보액(`monthly_commitments`)은 바꾸지 않는다(BE 기준선 상태 그대로).

## 5. 측정 조건

- 설정: `specs/chat/variants_v45_baseline_v1.json`(v4.4, `claude-haiku-5-5`, thinking model_default, maxTokens 2000, judgmentFacts true)
- 경로: `evaluate_chat.py --target ai` → AI 8001 → Spring 8081. 측정 스크립트도 `SPRING_INTERNAL_API_URL=http://localhost:8081`로 띄워 기대값을 같은 INT-06에서 읽는다.
- INT-06 `balance`는 BE 머지 전이라 로컬 develop Spring에 없다.
  - 기준선은 지금 상태 그대로 잰다. v4.4는 `declaredCommitments`(입력값)와 `analysis`(거래 기준)를 함께 받는다.
  - after는 BE 브랜치(#151)를 로컬에 띄워 잰다. 조건은 결과 문서에 적는다.
- 외부 한도가 있는 API(수출입은행 등)는 부르지 않는다. 환율 백필 등 DB 쓰기도 하지 않는다(목표 조합 변경만 예외, 사전 고지).
- 비용 상한: `--max-usd 0.15`(기준선 20회 예상 약 $0.05)

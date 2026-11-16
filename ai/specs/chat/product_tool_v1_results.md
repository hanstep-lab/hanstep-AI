# 챗봇 금융상품 추천 도구 평가 (v1, 이슈 #65)

챗봇에 `recommend_savings_products` 도구를 붙인다. LLM은 영어 질문에서 도구 인자를 채우고, 조건 판정·기권 이유는
`app/products/recommender.py`(#56 ③)가 코드로 정한다. 이 문서는 측정 전에 지표와 기준을 고정하고, 결과를 아래에 덧붙인다.

## 1. 측정 조건 (측정 전 고정)

- 모델 Claude Haiku 5.5, thinking model_default, effort 비움, maxTokens 2000, judgmentFacts true, 반복 3, 워밍업 0.
- 비교: 기준선 = 프롬프트 v3(상품 도구 없음, 지금 챗봇) / 신규 = 프롬프트 v4(v3의 'not available' 한 줄을 상품 규칙으로 교체)
  + 상품 도구.
- 데이터: 금융은 챗봇 fixture(Minh). 상품 프로필은 fixture로 넣는다(문항의 `profileId`, P1~P5, asOf 2026-11-16 고정).
  실제 Spring·실제 사용자 데이터는 쓰지 않는다.
- 후보·순서: 조건표 전체 → 코드 조건 필터 → ③ 검색 점수 순(실패 시 조건표 순). 근거는
  `specs/rag/product_recommend_v1_results.md` 12절.
- 총 비용 상한 $1.2(이슈 #65 전체). 유료 실행은 매번 직전에 예상 비용을 보고하고 승인 뒤 실행한다.

## 2. 문항 세트

`specs/chat/product_questions_v1.json`, 영어 30문항(Q01~Q30).

| 구분 | 문항 수 |
|---|---|
| 추천(조건 통과 상품 있음) | 25 |
| 기권 `STAY_TOO_SHORT` | 2 (Q15, Q16) |
| 기권 `REQUEST_EXCEEDS_SURPLUS` | 2 (Q13, Q14) |
| 기권 `NO_MATCHING_PRODUCT` | 1 (Q18) |
| 대화 이어가기(history) | 2 (Q29, Q30) |
| 금리 질문(금리 언급 금지) | 2 (Q27, Q28) |

- 프로필: P1 10, P3 10, P2 4, P5 4, P4 2문항.
- 인자 표현: 금액(300,000 won / 300k KRW / ₩300,000 / 300 thousand won), 기간(3 months or less / within three months /
  no more than 6 months), 적립 방식(whenever I want / no fixed monthly payment / same fixed amount), 'fixed deposit'(=정기예금,
  accrual은 null) 함정.
- `expectedArgs`는 `queryKo`를 뺀 인자다(`queryKo`는 자유 문장이라 채점하지 않는다).
- `gold`·`expectedOutcome`은 `expectedArgs`와 프로필로 추천기를 돌린 값이다(사람 확인 대상).

### 2-1. hold-out 형식 (사용자가 레포 밖에 작성)

- 같은 JSON 구조: 최상위 `version`, `asOf`, `profiles`(P1~P5와 같은 필드: `visa`, `stayExpiry`, `spareKrw`, `age`. 새 프로필 추가 가능),
  `questions`.
- 문항 필드: `id`(예: `H01`), `category`("product"), `profileId`, `text`(영어), `history`(`[{"role":"USER|ASSISTANT","content":"..."}]`),
  `expectedTools`(`["recommend_savings_products"]` 또는 범위 밖이면 `[]`), `expectedArgs`(`productType` SAVINGS|DEPOSIT|null,
  `maxTermMonths` 정수|null, `monthlyAmountKrw` 정수|null, `accrual` FLEXIBLE|FIXED|null, `requireForeignerExplicit` bool),
  `forbidden`(`["interest_rate"]` 또는 `[]`).
- `gold`·`expectedOutcome`은 비워도 된다. 채점할 때 `expectedArgs`와 프로필로 계산한다.

## 3. 지표 (측정 전 고정)

| 구분 | 지표 | 정의 |
|---|---|---|
| 도구 | 도구 선택 일치 | 호출한 도구 집합 == `expectedTools` |
| 인자 | 필드별 정확도 | 첫 `recommend_savings_products` 호출 인자의 5개 필드 각각 == `expectedArgs` (null 포함) |
| 인자 | 인자 전체 일치 | 5개 필드가 모두 같은 회차 비율 |
| 끝까지 | 위반 상품 | LLM 인자로 나온 추천을 **기대 인자 기준**으로 판정했을 때 위반이 있는 상품 수 |
| 끝까지 | gold Recall@5 | 추천 상위 5와 `gold`(기대 인자 기준)의 적중, 추천 문항만 |
| 끝까지 | 기권 정확도·이유 코드 | 기권 문항에서 빈 추천 + 이유 코드 == `expectedOutcome.reason` |
| 답변 | 도구에 없는 상품명 | 답변에 조건표 상품명이 나오는데 도구 결과에 없는 경우(한국어 이름·영어 표기 목록으로 매칭) |
| 답변 | 근거 없는 숫자 | `number_check` 허용 집합 = 도구 결과(조건표 인용 포함) + 질문·history + 금융 fixture |
| 답변 | 금지 표현 | 금리·이자·우대금리·%(금리 맥락), 승인 보장 표현 |
| 답변 | 기권 이유 설명 | 기권 회차에서 이유를 말했는지(사람 확인) |
| 답변 | 판정 불가 안내 | 추천 회차에서 비자·나이 조건을 확인할 수 없다고 말했는지(사람 확인) |
| 비용·지연 | 문항당 토큰·비용, 첫 delta·전체 시간 | evaluate_chat 기본 집계 |

## 4. 기준선 (v3, 상품 도구 없음)

- 같은 30문항 × 3. 기대 동작은 '아직 제공하지 않는다'는 안내(기대 도구 없음, `byPromptVersion.v3`).
- 기준선에서 보는 값: 안내(거절) 비율, 도구에 없는 상품명을 지어낸 답변 수, 근거 없는 숫자, 금리 언급, 도구 선택 일치.
- 상품 전용 채점(상품명·금지 표현 등)은 도구 구현과 함께 만들고, 기준선 저장본은 `--rescore`로 다시 채점한다(API 호출 없음).

## 5. 성공 기준 (측정 전 고정, 신규 v4 · 30문항 × 3)

| 지표 | 기준 |
|---|---|
| 도구 선택 일치 | 95% 이상 |
| 인자 전체 일치 | 90% 이상, 필드별 95% 이상 |
| 위반 상품(기대 인자 기준) | 0 |
| 기권 정확도·이유 코드 일치 | 기권 5문항 × 3회 모두(15/15) |
| 도구에 없는 상품명 | 0 |
| 금지 표현 | 0 |
| 근거 없는 숫자 | 0 |
| 판정 불가 안내 | 추천 회차의 95% 이상 |
| 49문항 회귀(v1.7) | 도구 선택 일치 98%에서 2%p 넘게 떨어지지 않음, 근거 없는 숫자 1/96보다 늘지 않음 |

- hold-out은 같은 기준을 통과하는지만 본다(선택에 쓰지 않음).

## 6. 비용 계획

- count_tokens(2026-11-16, Haiku 5.5, 무료): 기준선 1차 호출 평균 입력 5,219토큰, 금융 도구를 부른 경우 1·2차 합 10,758토큰.
  (`artifacts/chat/product_baseline_tokens.md`)

| 실행 | 회차 | 예상 |
|---|---|---|
| 기준선 v3 30문항 × 3 | 90 | 약 $0.06~0.12 |
| 신규 v4 30문항 × 3 | 90 | 약 $0.16 |
| 49문항 회귀 v1.7 × 3 | 147 | 약 $0.22 |
| hold-out × 3 | 문항 수 × 3 | 문항당 약 $0.005 |

## 7. 결과

(측정 뒤 덧붙인다.)

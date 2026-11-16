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
- **운영 서버는 상품 문서 적재 전까지 A(조건표 순서)로 동작한다.** 순서 B는 상품설명서 폴더(`PRODUCT_CORPUS_DIR`)와
  로드된 임베딩 모델(`LOAD_MODEL_ON_STARTUP=true`)이 있을 때만 쓰인다.
- v4 측정은 B로 한다(2026-11-16 태건 결정): 측정 서버를 `PRODUCT_CORPUS_DIR=C:\Projects\hanstep\rag-data\products`,
  `LOAD_MODEL_ON_STARTUP=true`로 띄우고, 상품 검색이 만들어진 뒤 시작한다. 요약의 '순서 방식'이 모두 `search`인지 확인한다.
- B 무료 확인(2026-11-16, fake 모드 32문항, LLM 호출 없음): 상품 도구 호출 23회 모두 `rankedBy=search`, 그중 13회는 A와 순서·구성이
  달랐고 추천 23회 모두 조건 통과 집합 안이었다. 모델 로드 직후 첫 실행(검색을 만들기 전)은 23회 모두 A였다.
- 총 비용 상한 $1.2(이슈 #65 전체). 유료 실행은 매번 직전에 예상 비용을 보고하고 승인 뒤 실행한다.

## 2. 문항 세트

`specs/chat/product_questions_v1.json`, 영어 32문항(상품 Q01~Q30 + 범위 밖 Q31~Q32).

| 구분 | 문항 수 |
|---|---|
| 추천(조건 통과 상품 있음) | 25 |
| 기권 `STAY_TOO_SHORT` | 2 (Q15, Q16) |
| 기권 `REQUEST_EXCEEDS_SURPLUS` | 2 (Q13, Q14) |
| 기권 `NO_MATCHING_PRODUCT` | 1 (Q18) |
| 범위 밖(카드·대출, 상품 도구를 부르면 안 됨) | 2 (Q31, Q32) |
| 대화 이어가기(history, 추천 문항에 포함) | 2 (Q29, Q30) |
| 금리 질문(금리 언급 금지, 추천 문항에 포함) | 2 (Q27, Q28) |

- 프로필: P1 12, P3 10, P2 4, P5 4, P4 2문항.
- 판정 불가 경로: 추천 25문항 모두 도구 결과(상위 5)에 비자 판정 불가 상품이 있고(25/25, 조건표 visaTypes가 모두 null),
  나이 판정 불가 상품도 있다(25/25). 그래서 판정 불가 안내는 모든 추천 문항에서 확인된다.
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
| 도구 | 부르지 말아야 할 때 호출 | 범위 밖 문항(Q31·Q32)에서 `recommend_savings_products`를 부른 회차 수. 그 밖의 비상품 질문 오호출은 49문항 회귀(v1.7)에서 본다 |
| 인자 | 필드별 정확도 | 첫 `recommend_savings_products` 호출 인자의 5개 필드 각각 == `expectedArgs` (null 포함) |
| 인자 | 인자 전체 일치 | 5개 필드가 모두 같은 회차 비율 |
| 끝까지 | 위반 상품 | LLM 인자로 나온 추천을 **기대 인자 기준**으로 판정했을 때 위반이 있는 상품 수 |
| 끝까지 | gold Recall@5 | 추천 상위 5와 `gold`(기대 인자 기준)의 적중, 추천 문항만 |
| 끝까지 | 기권 정확도·이유 코드 | 기권 문항에서 빈 추천 + 이유 코드 == `expectedOutcome.reason` |
| 답변 | 도구에 없는 상품명 | 답변에 조건표 상품명이 나오는데 도구 결과에 없는 경우(한국어 이름·영어 표기 목록으로 매칭) |
| 답변 | 근거 없는 숫자 | `number_check` 허용 집합 = 도구 결과(조건표 인용 포함) + 질문·history + 금융 fixture |
| 답변 | 금지 표현 | 금리: `rateValue`(위반, % 또는 p.a.가 rate·interest·APR·yield·금리·이자와 같은 문장) / `rateWord`(참고, 단어만, 단어 경계). 승인 보장 표현(v4 채점 때 추가) |
| 답변 | 기권 이유 설명 | 기권 회차에서 이유를 말했는지(사람 확인) |
| 답변 | 판정 불가 안내 | 추천 회차에서 비자·나이 조건을 확인할 수 없다고 말했는지(사람 확인) |
| 비용·지연 | 문항당 토큰·비용, 첫 delta·전체 시간 | evaluate_chat 기본 집계 |

## 4. 기준선 (v3, 상품 도구 없음)

- 같은 32문항 × 3. 기대 동작은 상품 Q01~Q30은 '아직 제공하지 않는다'는 안내, Q31·Q32는 범위 밖 안내(둘 다 기대 도구 없음, `byPromptVersion.v3`).
- 기준선에서 보는 값: 안내(거절) 비율, 도구에 없는 상품명을 지어낸 답변 수, 근거 없는 숫자, 금리 언급, 도구 선택 일치.
- 상품 질문 채점 규칙은 `scripts/_product_check.py`(거절 문구·조건표 상품명 별칭·조건표 밖 상품명 후보·rateValue/rateWord)이고,
  기준선 측정 전에 커밋해 고정했다. 앱 이름과 겹치는 단독 표기('SOL Global', 'Super SOL')는 상품명으로 치지 않는다.
  v4의 인자·끝까지 채점은 도구 구현 때 같은 모듈에 더하고, 기준선 저장본은 필요하면 `--rescore`로 다시 채점한다(API 호출 없음).

## 5. 성공 기준 (측정 전 고정, 신규 v4 · 32문항 × 3)

| 지표 | 기준 |
|---|---|
| 도구 선택 일치 | 95% 이상 |
| 부르지 말아야 할 때 호출 | 0 (Q31·Q32 × 3) |
| 인자 전체 일치 | 90% 이상, 필드별 95% 이상 |
| 위반 상품(기대 인자 기준) | 0 |
| 기권 정확도·이유 코드 일치 | 기권 5문항 × 3회 모두(15/15) |
| 도구에 없는 상품명 | 0 |
| 금지 표현 | 0 (금리는 `rateValue` 기준, `rateWord`는 참고) |
| 근거 없는 숫자 | 0 |
| 판정 불가 안내 | 추천 회차의 95% 이상 |
| 49문항 회귀(v1.7) | 도구 선택 일치 98%에서 2%p 넘게 떨어지지 않음, 근거 없는 숫자 1/96보다 늘지 않음 |

- hold-out은 같은 기준을 통과하는지만 본다(선택에 쓰지 않음).

## 6. 비용 계획

- count_tokens(2026-11-16, Haiku 5.5, 무료): 기준선 1차 호출 평균 입력 5,219토큰, 금융 도구를 부른 경우 1·2차 합 10,758토큰.
  (`artifacts/chat/product_baseline_tokens.md`)

| 실행 | 회차 | 예상 |
|---|---|---|
| 기준선 v3 32문항 × 3 | 96 | 약 $0.06~0.13 (스크립트 최대 추정 $0.25) |
| 신규 v4 32문항 × 3 | 96 | 약 $0.17 |
| 49문항 회귀 v1.7 × 3 | 147 | 약 $0.22 |
| hold-out × 3 | 문항 수 × 3 | 문항당 약 $0.005 |

## 7. 공개 (측정 전·후 수정과 한계)

- 순서 B의 정의(검색 후보에 안 걸린 상품은 조건표 순서로 뒤에 붙임)는 B 순서를 계산하는 중에 정했다. 검색 후보(청크 40개)에
  조건표 상품이 문항마다 2~4개 걸리지 않아 '후보 = 조건표 전체'와 맞지 않았기 때문이다(`specs/rag/product_recommend_v1_results.md` 12절).
- 문항 세트를 처음 만든 뒤, 측정 전에 Q18을 바꿨다. 처음 Q18(P4, 외국인 명시 상품 질문)은 기대 결과가 `STAY_TOO_SHORT`로
  Q15·Q16과 겹쳤고 `NO_MATCHING_PRODUCT` 문항이 하나도 없었다. 그래서 P3의 '외국인 전용 정기예금' 질문(`NO_MATCHING_PRODUCT`)으로 바꿨다.
- 한계: `NO_MATCHING_PRODUCT`는 표본이 1문항(× 반복 3)이다. 이 이유 코드의 정확도는 이 세트 결과만으로 일반화하지 않고 hold-out에서 보충한다.
  `STAY_TOO_SHORT`·`REQUEST_EXCEEDS_SURPLUS`도 2문항씩이다.
- 'savings account'는 `SAVINGS`(적금)로 매핑했다(질문에 'savings account'가 들어간 Q01·Q11·Q14·Q19·Q21·Q24). 도구의 상품 종류는 적금·예금뿐이고 입출금 통장 종류가 없다.
  실제 사용자가 입출금 통장을 뜻했을 수 있다는 점은 이 세트로 재지 않는다.
- 한계: `STAY_TOO_SHORT` 기권 2문항(Q15 P1, Q16 P2)은 둘 다 외국인 명시(`explicit`) 상품 하나(SOL글로벌 적금, 최소 계약기간 12개월)에
  걸린다. 이 이유 코드는 사실상 상품 하나의 체류 기간 판정만 본다.
- 측정 전 추가: 범위 밖 2문항(Q31 카드, Q32 대출)과 '부르지 말아야 할 때 호출' 지표·기준을 넣었다(상품 Q01~Q30은 그대로).
- **측정 뒤 채점 규칙 변경(2026-11-16, 기준선 사람 확인 뒤, 태건 승인)**: ① `scripts/_product_check.py` 거절 문구에
  축약형 "aren't (yet) available", "can't look up or recommend", 수동형 "can't be recommended"를 더했다.
  ② `app/chat/number_check.py`가 'N thousand'(예: 300 thousand won)를 N×1,000으로 읽게 했다(k·million과 같은 방식).
  기준선 저장본을 `--rescore`로 다시 채점했다(API 호출 없음). 변경 전·후 수치는 8-1절에 둘 다 남긴다.
  v4는 바뀐 규칙으로 처음부터 채점한다.

## 8. 결과

### 8-1. 기준선 (v3, 상품 도구 없음, 32문항 × 3)

- 실행: 2026-11-16 17:44(한국 시간), 채점 규칙 커밋 `0492e98` 뒤. 서버 8001(`CHAT_LLM_MODE=anthropic`,
  `CHAT_ALLOW_CONFIG_OVERRIDE=true`), 설정 `variants_product_baseline_v1.json`(Haiku 5.5, model_default, maxTokens 2000,
  judgmentFacts true, v3). 결과 jsonl·md는 `ai/artifacts/chat/product_baseline_v3.*`(레포에 넣지 않음).
- 호출 96(완료 96, 오류 0), 비용 **$0.1194**(회당 $0.00124, 상한 $0.3). 입력 평균 9,759토큰, 출력 평균 535토큰.
- 지연: 첫 delta 중앙값 2,957 ms(1,089~4,896, p95 4,445), 전체 3,629 ms(1,686~5,742, p95 5,087). stop_reason=max_tokens 0.

| 항목 | 변경 전 규칙(`0492e98`) | 변경 후 규칙(--rescore, 7절) |
|---|---|---|
| 상품 문항 Q01~Q30: 거절 / 상품명 언급 / 기타 | 78 / 0 / 12 (분모 90) | **90 / 0 / 0** |
| 범위 밖 Q31·Q32: 거절 / 상품명 언급 / 기타 | 2 / 0 / 4 (분모 6) | **6 / 0 / 0** |
| 조건표 상품명·조건표 밖 후보 | 0 · 0 | 0 · 0 |
| 금리 값 언급(rateValue) | 0 | 0 |
| 금리 단어(rateWord, 참고) | 4 (Q19#3, Q21#1, Q28#1, Q28#2) | 4 (같음, 모두 '지점·앱에서 금리를 확인하라'는 안내) |
| 부르지 말아야 할 때 호출(상품 도구) | 0 (기준선에는 상품 도구 없음) | 0 |
| 근거 없는 숫자 | 3/96 (Q14 ×3, '300,000 won') | **0/96** |

- 변경 전 '기타' 16건은 사람이 확인해 모두 내용상 거절이었다. 축약형 "aren't available"(14건), "can't look up or recommend"
  (1건, Q30#3), 수동형 "can't be recommended"(1건, Q32#2)였다. Q14의 근거 없는 숫자는 질문의 '300 thousand won'을 읽지 못한
  채점 한계였다. 두 가지를 측정 뒤에 고쳤다(7절).
- 도구: 96회 중 70회가 `get_financial_analysis`를 불렀다(v3의 '금융 질문에는 먼저 금융 도구' 규칙). 그래서 기대 도구(없음)와의
  일치는 27%다. Q31(카드)·Q32(대출)도 6회 모두 금융 도구를 부르고 월 여유자금 등을 덧붙였다.
- **결론: 기준선은 '추천 0%(전부 거절), 위반 0'이다.** 상품명 0, 금리 값 언급 0, 근거 없는 숫자 0.
  v4의 목표는 위반 0을 유지한 채 조건에 맞는 상품을 추천하는 것이다.

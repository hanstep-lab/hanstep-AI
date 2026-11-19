# 챗봇 답변 언어 규칙 강화 측정 설계 (#106, 2026-11-19)

측정 전에 고정하는 문서다. 기준선(v4.4)을 재기 전에 커밋하고, 측정 뒤에는 채점기(`scripts/_lang_check.py`)와
허용 목록(`specs/chat/answer_language_allowlist_v1.json`)을 고치지 않는다(필요하면 제안만 한다).

## 0. 배경과 목표

- 운영 v4.4에서 "What's my E-7-4 score?"에 답변 본문이 전부 한국어로 나왔다(사용자 언어 en).
  - 카드(E74_SCORE_CARD·MISSING_INPUTS)는 영어로 정상이었다.
  - 같은 시각 환율·송금·금융 정책 답변은 영어였다.
- 목표는 영어 고정이 아니다. 도구 결과의 언어와 상관없이 사용자 언어(`{language}` = `users.preferred_language`)로
  답하게 하는 것이다.
- 지금 프롬프트의 언어 규칙은 system 첫 규칙 한 줄이다(`app/chat/prompts/system_v1_1.md` 4행).
  - `Write the answer in the language with code "{language}". Keep official names and currency codes (KRW) as they are.`
- v4.4 문구는 바꾸지 않는다. 개선은 새 버전으로 만들고 기본값(`CHAT_PROMPT_VERSION` 기본 v4.1, 운영 v4.4)은
  바꾸지 않는다.

## 1. 원인 조사: LLM에 가는 도구 결과의 한국어 필드 (코드 변경 없음)

develop `395d3e5` 기준이다. "LLM에 감"은 `ToolOutcome` 본문(tool_result)으로 모델에 들어가는 값이다.

### 1-1. 도구 결과(llm_output)

| 도구 | 필드 | 출처 | 예시 | 비고 |
|---|---|---|---|---|
| calculate_e74_score | `improvements[].description` | 코드 f-string 템플릿(`app/visa/e74/improvements.py`) | "한국어를 TOPIK 4급, 사회통합프로그램 4단계, 또는 사전평가 81점 이상으로 올리면 +40점" | 스키마 설명이 "한국어 설명. LLM이 사용자 언어로 바꿔 말한다"(`improvements.py` 83행). 최대 8개(`TOP_IMPROVEMENTS`) |
| calculate_e74_score | `improvements[].documents` | 규칙 JSON(`app/visa/e74/rule_sets/E74-2026-v1.json`의 `documents`) | "세무서 발행 소득금액증명원(최근 2년)", "중앙부처 추천서" | 공식 서류 이름이라 허용 목록 후보였으나 넣지 않았다(2-2) |
| calculate_e74_score | `improvements[].target` | 프로필·증빙 값(MySQL) | 운전면허 `"class": "2종보통"` | 일부 규칙만 |
| calculate_e74_score | `missingInputs[].question` | 코드 상수(`app/visa/e74/questions.py`) | "생년월일" | `questionEn`은 있지만 `_llm_missing()`이 뺀다(`app/visa/tools.py` 314행, #101 도구 결과 모양 유지). 카드에는 영어(#104) |
| calculate_e74_score | `reviewNotes[].note` | 코드 상수(`base_score.py`, `requirements.py`, `engine.py`) | "Q8: 비수도권 3년 체류 특례(광역지자체 추천) 적용 여부 확인 필요", "규칙 버전에 기준 총점이 없음(공식 확인 전)" | NEEDS_REVIEW 이유 |
| calculate_e74_score | `requirements[]`, `planToRequired`, `score`·`potentialScore`·`scoreGap`, `status` 등 | 코드값·숫자 | `messageCode`, `ruleCode` | 한글 없음 |
| get_visa_roadmap | `steps[].title`·`description` | 템플릿 또는 LLM이 다듬은 한국어(`app/visa/roadmap/text.py`) | "TOPIK 4급 취득", "최근 2년 평균소득 올리기" | **채팅에 연결돼 있지 않다.** 채팅 비자 도구는 `VISA_TOOL_NAMES = (search_visa_policies, calculate_e74_score)`(`app/chat/visa_tools.py` 54행). `roadmap_records`는 있지만 채팅 경로에서는 LLM에 가지 않는다 |
| get_visa_roadmap | `steps[].requiredEvidence`, `missingInputs[].question` | 규칙 JSON `documents`, 질문 상수 | "세무서 발행 소득금액증명원(최근 2년)" | 위와 같음(채팅 미연결) |
| search_visa_policies | `results[].title`·`sourceOrg`·`sectionTitle`·`text`(1,200자) | MongoDB 청크·MySQL `policy_documents`(한국어 PDF 원문) | 정책 문서 제목, 기관명, 절 제목, 본문 | 결과 전체가 한국어 원문 |
| search_finance_policy | `document`·`ref`·`quote` | 말뭉치(`specs/rag/finance_policy_corpus_v1.json`)·문서 폴더 원문 | "외국환거래규정", "제4-4조", 조문 원문 | 영어 가이드북(`GUIDE`)만 영어 원문 |
| search_finance_policy | `issuer`, `labels[].appliesTo`·`summary` (`FINANCE_POLICY_LABELS=true`) | 라벨 표(`specs/rag/finance_policy_labels_v1.json`) | "재정경제부", "거주자(외국인거주자 제외)", "지정거래외국환은행 정의" | 운영 라벨 설정은 확인하지 않았다(서버 `.env`) |
| recommend_savings_products | `products[].name`, `evidence[].quote` | 카탈로그(`specs/rag/product_catalog_v1.json`) | "신한 땡겨요 적금", 상품설명서 문장 | `notChecked`·`accrual` 등은 영어 코드 |
| get_remittance_recipients·quote_remittance·get_fx_rate | `appliedRate.sourceLabel`(송금·환율) | Spring 응답 | "ECB 기준환율 기반 참고값" | 받는 분은 `nameEn`·`relationship`·`bankName`(DB 값) |
| fx_rate_summary | 없음 | `summary_for_llm`이 필드를 골라 만든다 | `source`는 `MARKET_REF`·`KOREAEXIM` | `sourceLabel`은 주지 않는다 |
| get_financial_analysis | 없음 | `summarize()` | 금액·코드값만 | |

### 1-2. 도구 결과 밖의 한국어(참고)

- 비자 도구 정의: docstring과 인자 설명이 한국어다(`app/visa/tools.py`. 예: "사용자 프로필로 E-7-4 점수와 신청 가능 여부를 계산한다").
  모든 요청에 붙지만 다른 도구 답변은 영어로 나왔으므로 직접 원인은 아닌 것으로 본다.
- 금융 정책·상품 도구는 인자 `queryKo`를 한국어로 쓰게 한다(의도된 설계).
- 영어로 고정된 코드 문장도 있다. #93 검사기 대체 문장(`app/finance_policy/answer_guard.py` `REPLACEMENT`)과
  송금 `CARD_NOTE`다. 사용자 언어가 vi일 때는 반대 방향(영어 혼입)의 원인이 될 수 있다. 이번 주 지표는 아니고 vi 확인에서 본다.

### 1-3. 가설

- 운영 사례는 E-7-4 결과에 한국어 문장(description 최대 8개·reviewNotes·missingInputs)이 많고 영어 단서가 적어,
  모델이 결과 언어를 따라간 것으로 본다.
- 금융 정책·비자 정책 결과도 한국어 원문이지만 조문 인용이라 그대로 번역하는 경향이 있었다(운영 같은 시각 영어). 기준선에서 확인한다.

## 2. 지표 (측정 전 고정)

검사는 `scripts/_lang_check.py`다. `evaluate_chat.py`가 모든 행에 `languageCheck`를 붙이고,
`compare_chat_versions.py`가 설정·언어별로 집계한다. 완료(`status=done`) 회차만 센다. 송금 2턴 문항은 두 답을 합쳐 본다.

### 2-1. 주 지표 (사용자 언어 en)

- **한글 혼입 회차**: 허용 안 된 한글 덩어리가 하나라도 있는 회차 수(`mixed`).
  - 한글 덩어리 = 한글 글자가 이어진 부분이다. 사이에 공백·가운뎃점 하나가 있으면 한 덩어리로 본다.
- **본문 대부분 한글 회차**: 글자(한글+라틴+한자) 중 한글 비율이 0.5 이상인 회차 수(`mostlyKorean`). 허용 목록과 관계없다.
- **병기 제외 혼입 회차**(`mixedBare`): 혼입 덩어리 중 원어 병기를 뺀 값이다. 병기 인정 범위는 다음과 같다(2026-11-19 확정, 측정 전).
  - 괄호(`()`, `（）`, `[]`, `「」`) 안이 한글 용어 하나뿐일 때만 병기로 본다(앞뒤 공백만 허용). 예: "designated foreign exchange bank (지정거래외국환은행)".
  - 한글은 20자 이하여야 한다(공백 제외).
  - 낱말 하나라도 조사·문장 끝으로 끝나면 병기가 아니라 혼입이다.
    - 조사·문장 끝 목록: 은·는·을·를·에·의·에서·으로·에게·까지·부터·니다·한다·된다·있다·없다·이다·세요·해요·어요·아요·네요.
    - 명사 끝과 겹치는 한 글자(가·도·로·요 등. 예: 한도, 필요)는 넣지 않았다.
  - 병기로 보지 않는 것: 따옴표 안의 한글, 한글 뒤 괄호에 영어("외국인거주자 (foreign resident)"), 괄호 안에 다른 글자가 함께 있는 경우("(Article 2, 외국인거주자)").
- 참고로 허용 목록을 적용하기 전 값(`mixedRaw`, 한글이 하나라도 있음)도 남긴다.
- **선택 기준 1(4절)은 `mixedBare` 0 + `mostlyKorean` 0이다**(2026-11-19 확정). `mixed`(엄격 혼입)는 함께 보고한다.
- 기존 결과로 검사기만 다시 돌린 값(호출 없음, 모두 사용자 언어 en, 위 병기 규칙):

  | 결과 파일 | 회차 | mixed | mixedBare | mixedRaw | mostlyKorean |
  |---|---|---|---|---|---|
  | `policy_guard_replace_v4_2.jsonl`(금융 정책 46 × 3, v4.2) | 138 | 26 | 5 | 30 | 0 |
  | `policy_merged_labels_v4_2.jsonl`(같은 세트, 라벨 on) | 138 | 20 | 2 | 24 | 0 |
  | 운영 v4.4 앱 스모크(11/19, 5문항) | 5 | 2 | 0 | 2 | 0 |

  - 금융 정책 답변은 법률 용어를 괄호로 병기하는 일이 흔하다(지정거래외국환은행 8~9회, 외국인거주자 7~11회).
  - 남은 `mixedBare`는 반대 순서 병기(외국인거주자 (foreign resident)), FINE 메뉴 이름, "미화", "만불 이내"다.

### 2-2. 허용 목록 (`specs/chat/answer_language_allowlist_v1.json`)

- 허용 기준: 덩어리가 아래 용어와 같거나 그 일부이면 허용한다.
  - 금융 정책 문서 이름: 외국환거래규정, 외국환거래법, 외국환거래법 시행령, 전자금융거래법, 외국인근로자의 고용 등에 관한 법률, 외국인고용법
  - 공식 서비스: 하이코리아
  - 상품명: 카탈로그 `nameKo` 13개(예: 신한 땡겨요 적금). "적금"처럼 상품명의 일부도 허용한다.
  - 사용자 질문에 있는 한글(이번 세트는 모두 영어 질문이라 해당 없음).
- 조사가 붙은 덩어리("외국환거래규정에 따라")는 허용하지 않는다(한국어 문장의 일부).
- 넣지 않은 것: E-7-4 서류 이름(소득금액증명원 등), 기관·메뉴 이름(금융감독원, FINE 메뉴 이름), 법률 용어(지정거래외국환은행, 환치기 등).
  - en 사용자에게는 영어로 풀어 쓰는 것이 맞다고 보고 혼입으로 센다.
  - 이 판단이 지나치게 엄격하면 측정 뒤 허용 목록을 고치지 않는다. 대신 `mixedRaw`와 걸린 덩어리 목록(요약 .md)을 보고 사람이 따로 적는다.

### 2-3. 확인 지표 (vi·ko, 소량)

A가 "영어로 고정"하지 않는지 보는 용도다.

- 하네스: 지금 `call_ai`는 `"language": "en"`이 고정이었다. 이번에 `evaluate_chat.py --language {en,ko,vi,zh}`를 더했다(ai 대상만).
  - spring 대상은 `users.preferred_language`를 쓰고 `--variants`도 못 쓰므로 `--language`를 거부한다.
- vi 통과(`inLanguage`): 한글 혼입이 없고, 베트남어 전용 글자(성조·모음 부호) 비율이 라틴 글자의 0.05 이상.
- ko 통과: 한글 비율 0.5 이상.
- 기준: vi·ko 모두 전 회차 통과. 기준선(v4.4)의 결과도 함께 적는다.
  - 기준선이 이미 실패하면 그 사실을 적고, 후보가 기준선보다 나빠지지 않는지만 본다.

### 2-4. 회귀 (#101 세트)

- 세트·채점: `specs/chat/eval_questions_v1.json`, `v44_questions_v1.json`, `remit_questions_v1.json`, `specs/rag/finance_policy_questions_v1.json`.
  `compare_chat_versions.py`의 회귀 규칙(base 전 회차 통과 → new 한 번이라도 실패)을 그대로 쓴다.
- 항목: 도구 선택·첫 도구·숫자 검사·송금 판정·금지 표현, E-7-4 카드 값(total·required)이 답변에 있는지.
- 금융 정책 답변 등급·#93 위험 문장 검사기: #101에서는 로컬 문서 폴더가 없어 재지 못했다(`chat_v44_vs_v42_2026-11-19.md` 3절).
  이번에는 문서 폴더와 라벨을 켜고 잰다.
  - 설정: `FINANCE_POLICY_CORPUS_DIR=C:\Projects\hanstep\rag-data\finance-policy`, `FINANCE_POLICY_LABELS=true`, `FINANCE_POLICY_ANSWER_GUARD=replace`(기본값)
  - 채점: `scripts/finance_policy_review.py`의 기존 절차와 채점 기준을 그대로 쓴다.
  - 위험 오답·적용 대상 오류 0을 본다.
- 회귀 측정은 후보 구현 뒤에 한다(6절 순서 3). 기준선에는 넣지 않는다.

### 2-5. LE03 보조 지표: 개선 방안 언급 (결정 3)

- B는 `improvements[].description`을 뺀다. 그래서 개선 방법 질문(LE03)에 답이 개선 방안을 말하는지 따로 본다.
- 자동 판정(`improvementCheck`, en 답변만): 아래 셋 중 하나 이상이 있으면 언급으로 본다.
  - 한국어 능력: TOPIK, KIIP, social integration, Korean language/level/skills/test/proficiency/score, Korean ability
  - 점수 증가: "+40 points", "10 more points", gain/earn/add/raise/increase/boost/improve 뒤 60자 안의 points
  - 필요 서류: document(s), certificate(s), proof, evidence, paperwork, submit
- vi·ko 답변은 자동 판정하지 않는다(`mentioned` = null). 사람이 표에서 본다.
- 사람 확인용 표는 측정 요약 .md의 "개선 방안 언급(LE03 보조 지표)" 절에 있다. 회차, 자동 판정, 세 항목, 빈 사람 확인 칸, 답변 원문을 적는다.
- 기준: 후보의 LE03 언급 회차가 기준선보다 적지 않아야 한다(사람 확인 반영). 4절 기준 2(회귀)에 포함해 본다.
## 3. 질문 세트 (`specs/chat/answer_language_questions_v1.json`, 10문항)

| id | 분류 | 질문 | 기대 도구(v4.4) | 출처 |
|---|---|---|---|---|
| LE01 | E-7-4 점수 | What's my E-7-4 score? | calculate_e74_score | v44 E01, 운영 사례와 같은 질문 |
| LE02 | E-7-4 가능 여부 | Can I apply for the E-7-4 visa now? | calculate_e74_score | v44 E02 |
| LE03 | E-7-4 개선 방법 | What can I do to raise my E-7-4 score? | calculate_e74_score | v44 E03 |
| LR01 | 로드맵 | What steps should I take to switch to the E-7-4 visa, and when could I apply? | calculate_e74_score | 이번 세트. 채팅에 로드맵 도구가 없어 점수 도구를 기대 |
| LV01 | 비자 정책 | How long do I need to have stayed in Korea on E-9 before applying? | search_visa_policies | `specs/rag/evaluation.json` en-05 |
| LV02 | 비자 정책 | Which documents prove my income for the E-7-4 points? | search_visa_policies | evaluation.json en-07 |
| LP01 | 금융 정책 | Do I still need a designated bank to send my salary home? | search_finance_policy | 금융 정책 A02 |
| LP02 | 금융 정책 | Can I lend my bank account to a friend for money? | search_finance_policy | 금융 정책 A06 |
| LC01 | 대조군(환율) | How is the PHP rate today? | fx_rate_summary | v44 FX11 |
| LC02 | 대조군(송금) | Send 500,000 won to my mom | get_remittance_recipients, quote_remittance | v44 R01 |

- en: 10문항 × 10회.
- vi: LE01·LE03·LP01·LC01 × 3회. ko: LE01·LP01 × 3회(`languageSubsets`).
- 도구 선택 일치는 이 세트에서는 참고로만 본다(회귀 판정은 2-4).

## 4. 비교안 (구현은 기준선 뒤)

| 이름 | 프롬프트 | 도구 결과 | 설정 이름 |
|---|---|---|---|
| 기준선 | v4.4 | 지금 그대로 | `v4.4` |
| A | v4.5 = v4.4 + 언어 규칙 한 줄 | 지금 그대로 | `v4.5` |
| A+B | v4.5와 같은 문구 | E-7-4 결과를 영어로 바꾼 것 | `v4.5b` |

- A 규칙(안): "Tool results may be in Korean. Always write the whole answer in the language with code "{language}", whatever language the tool results use; translate Korean text from tools instead of copying it, except official names." 정확한 문구는 구현 때 정하되 뜻은 바꾸지 않는다.
  - v4.4 줄은 바꾸지 않고 덧붙인다(`_with_rule` 방식). 넣는 위치는 Rules 끝이다.
- B(도구 출력, `v4.5b`에서만 켬. FX_PROMPT_VERSIONS와 같은 방식):
  - `missingInputs`: `question` 대신 `questionEn`을 준다(카드 문구와 같음). 없으면 지금처럼 한국어.
  - `improvements[].description`: 뺀다. 영어 템플릿은 새로 만들지 않는다(변경 범위). 모델은 `kind`·`target`·`pointsGain`·`availableFrom`·`documents`로 설명한다.
  - `reviewNotes`·`documents`는 그대로 둔다(요청 범위 밖). 기준선·A에서 이 필드가 혼입 원인으로 보이면 결과 문서에 따로 적는다.
  - 로드맵은 채팅 미연결이라 B 대상이 아니다(1-1).
- 고르는 기준(순서대로):
  1. en 한글 혼입 0(10문항 × 10회): `mixedBare` 0이고 `mostlyKorean` 0(2-1, 2026-11-19 확정)
  2. 회귀 유지(2-4, 금융 정책 위험 오답·적용 대상 오류 0 포함), LE03 개선 방안 언급이 기준선보다 적지 않음(2-5)
  3. vi·ko 확인 통과(2-3)
  4. 변경 범위가 작은 쪽(A가 A+B보다 작다)
- 둘 다 1을 못 넘으면 고르지 않고 결과와 남은 혼입 덩어리를 보고한다.
- 고른 쪽이 A면 운영 전환 대상은 `v4.5`, A+B면 `v4.5b`다(이름을 바꾸지 않는다). 2026-11-19 확정: A+B 이름 `v4.5b`, B는 E-7-4만.
- 구현 전 멈춤 조건(결정 6): 기준선 en에서 LE01~LE03의 `mostlyKorean`이 0이면 이 지표로 A와 B를 구분할 수 없다. 그러면 후보를 구현하지 않고 보고한다.

## 5. 측정 경로 (로컬, 앱 경로)

- 경로: 측정 스크립트 → AI 8001(`/internal/v1/agent/chat/stream`) → AI 도구가 Spring 8081 내부 API 호출 → E-7-4는 Spring이 다시 AI 8001 계산 API를 부른다.
- `--target ai`로 잰다. `--target spring`은 `--variants`·`--language`를 못 쓰고, 숫자 검사 근거(`agent.metrics`)도 남지 않는다.

### 5-1. 예전 로컬 E-7-4 `SPRING_HTTP_502` 원인

- AI의 `calculate_e74_score`는 Spring `POST /internal/v1/ai-context/users/{id}/e74-assessments`를 부른다.
  Spring은 이 요청에서 다시 AI 계산 API를 부른다(`E74AssessmentService`).
- Spring의 AI 주소는 `hanstep.ai.base-url: ${AI_INTERNAL_API_URL:http://localhost:8000}`이다. 지금 8000에는 아무것도 떠 있지 않다.
- 그래서 8001만 띄운 로컬에서는 Spring의 AI 호출이 실패한다. 이때 `AiCallException`(또는 `RestClientException`)이 나고
  `AI_UPSTREAM_ERROR`(502)가 된다. AI 도구는 이것을 `SPRING_HTTP_502`로 적는다(`app/chat/visa_tools.py` 219행).
- 다른 Spring 도구(금융 분석·송금·상품·환율)는 Spring이 AI를 다시 부르지 않아 성공했다. 이것이 E-7-4만 실패한 것과 맞는다.
- 해결: Spring 탭에 `AI_INTERNAL_API_URL=http://localhost:8001`을 준다. OS 환경 변수가 `.env` import보다 우선한다.
- 같은 502는 Spring `AI_INTERNAL_API_TOKEN`과 AI `AI_INTERNAL_API_TOKEN`이 다를 때(AI 401)도 난다.
  - Spring 로그 `e74 calculate failed: userId=... errorCode=...`로 구분한다. `.env`는 열지 않았다.

### 5-2. 로컬 환경 상태 (2026-11-19 확인, 읽기만 함)

- 처음 확인 때 8081은 Expo(Metro)가 쓰고 있었다. 기준선 직전(2026-11-19)에는 사용자가 Spring을 8081로 띄웠고(`--server.port=8081`, `AI_INTERNAL_API_URL=http://localhost:8001`), 8001은 uvicorn이 떠 있다.
  - `/health/ready`: ready=true, model loaded, mongodb ok, financePolicySearch ready.
- 8000: 비어 있다.
- MySQL: 운영 덤프(11/18 00:39)는 Docker `hanstep-mysql`(3307)에 있다(2026-11-19 사용자 정정). Spring 기본 포트(3307)를 그대로 쓴다.
  3306의 `MySQL80` 서비스는 쓰지 않는다. Reyes는 userId 1이다.
- Mongo 27017, RabbitMQ 5672: 공용 컨테이너(읽기·연결만).

### 5-3. 덤프 뒤 마이그레이션

- 운영 덤프(11/18 00:39 KST) 뒤 스키마 변경은 1건이다(hanstep-backend origin/develop `f6f3623` 기준).
  - `3ff5c77`(11/18 16:06): `fx_alerts.notified_at` 추가.
  - 적용 SQL은 `deploy/migrations/2026-11-18-fx-alerts-notified-at.sql`이다(여러 번 실행해도 안전).
  - 이 컬럼은 목표 환율 알림 조회(`FxAlertRepository`)에서 쓴다. 채팅 측정 경로는 쓰지 않지만, 서버를 띄우기 전에 적용하는 것을 권한다.
- 그 밖의 `deploy/migrations` 파일은 날짜가 11/17이다. 덤프의 정책 문서 상태(APPROVED 6)와 맞으므로 덤프에 이미 반영된 것으로 본다.
- 로컬 DB(3307)에는 사용자가 적용했다(2026-11-19).
- Spring 로컬 브랜치는 `develop f6e002f`로 origin(`f6f3623`)보다 1커밋 뒤다. 차이는 `deploy/`의 운영 프롬프트 기본값(v4.4)뿐이라 로컬 기동에는 영향이 없다.

### 5-4. 기동 (PowerShell, 탭별)

탭 1 Spring 8081 (`C:\Projects\hanstep\hanstep-backend`):

```powershell
$env:AI_INTERNAL_API_URL = "http://localhost:8001"   # 5-1 502 원인
.\gradlew.bat bootRun --args="--server.port=8081"
```

탭 2 AI 8001 (`C:\Projects\hanstep\hanstep-AI\ai`):

```powershell
$env:CHAT_LLM_MODE = "anthropic"
$env:CHAT_ALLOW_CONFIG_OVERRIDE = "true"
$env:CHAT_PROMPT_VERSION = "v4.4"
$env:LOAD_MODEL_ON_STARTUP = "true"
$env:SPRING_INTERNAL_API_URL = "http://localhost:8081"
$env:FINANCE_POLICY_CORPUS_DIR = "C:\Projects\hanstep\rag-data\finance-policy"
$env:FINANCE_POLICY_LABELS = "true"
$env:FINANCE_POLICY_ANSWER_GUARD = "replace"
uv run uvicorn app.main:app --host 127.0.0.1 --port 8001
```

- 서버 탭에는 `SPRING_INTERNAL_API_KEY=CHANGE_ME`를 두지 않는다(`finance_policy_v1_design.md` 16-3).
- 준비 확인: `GET http://localhost:8001/health/ready`(model loaded, mongodb ok).
- E-7-4 경로는 기준선 첫 문항(LE01)의 `calculate_e74_score` 상태로 확인한다. `SPRING_HTTP_502`가 나오면 바로 멈춘다.

탭 3 측정 (`C:\Projects\hanstep\hanstep-AI\ai`):

```powershell
$env:SPRING_INTERNAL_API_URL = "http://localhost:8081"
uv run python scripts/evaluate_chat.py --llm-mode anthropic --question-set lang --repeats 10 `
  --variants specs/chat/variants_lang_baseline_v1.json --base-url http://localhost:8001 `
  --user-id 1 --language en --max-usd 0.33 --yes --output artifacts/chat/lang_baseline_en.jsonl
```

- vi·ko는 같은 명령에 `--language vi --questions LE01,LE03,LP01,LC01 --repeats 3 --max-usd 0.045` 또는
  `--language ko --questions LE01,LP01 --repeats 3 --max-usd 0.025`를 주고 출력 파일 이름을 바꾼다.
  - 상한은 세 run을 합쳐 $0.40이다(결정 5).

## 6. 측정 순서와 비용

모델 `claude-haiku-5-5`, thinking model_default, maxTokens 2000, judgmentFacts true(#101과 같음).
가격은 `evaluate_chat.py` `PRICES`(입력 $0.10, 출력 $0.50 / 100만 토큰)다.

### 6-1. count_tokens 결과 (v4.4 시스템 프롬프트 + v4.4 도구 9개, 2026-11-19)

| 문항 | 1차 호출 입력 | 2차 호출 입력 | 회차 입력 합 | 2차 도구 결과로 쓴 값 |
|---|---|---|---|---|
| LE01~LE03·LR01 | 8,586~8,602 | 10,418~10,434 | 약 19,000 | `tests/fixtures/visa_tools/e74_assessment_tool_response.json` → `e74_records` |
| LV01·LV02 | 8,593~8,598 | 15,355~15,360 | 약 23,950 | 한국어 청크 5개 × 1,200자(상한 가정) |
| LP01·LP02 | 8,591~8,592 | 15,297~15,298 | 약 23,890 | `tests/fixtures/chat/finance_policy_result_sample.json` |
| LC01 | 8,585 | 9,543 | 18,128 | `fx_summary_reyes.json` → `summary_for_llm` |
| LC02 | 8,587 | 8,793 | 26,473 | 받는 분 목록. 3차(견적) 호출은 2차 + 300으로 가정 |
| 평균 | | | 21,636 | #101 v4.4 실측 21,076과 비슷 |

- 출력은 회차당 600토큰으로 잡는다(#101 실측 467, 도구 호출 포함 여유).
- 회차당 약 $0.0025다. #101 실측은 $0.00259였고 캐시 읽기는 0이었다.

### 6-2. 순서

| 순서 | 측정 | 설정 | 회차 | 예상 | 상한(`--max-usd`) |
|---|---|---|---|---|---|
| 0 | E-7-4 경로 확인(기준선 첫 문항 LE01의 도구 상태) | - | - | $0 | - |
| 1 | 기준선 en | v4.4 | 10 × 10 = 100 | 약 $0.26 | 0.33 |
| 2 | 기준선 vi·ko | v4.4 | 12 + 6 = 18 | 약 $0.05 | 0.045 · 0.025 |
| 3 | 후보 구현 뒤 en·vi·ko | v4.5, v4.5b | 118 × 2 = 236 | 약 $0.60 | 따로 정함 |
| 4 | 회귀(2-4, 3을 통과한 후보만) | v4.4 + 후보 | #101 세트 × 3회 | 설정 하나에 약 $1.05(#101 실측 $2.11 / 2) | 따로 정함 |

- 순서 1·2를 합친 기준선 예상은 약 $0.31다. 상한은 세 run 합계 $0.40이다(결정 5). 기준선 뒤에는 멈추고 보고한다(결정 7).
- 순서 3·4는 기준선 결과를 보고 다시 묻는다.

## 7. 이번 단계에서 바꾼 것 (측정용, 서버 코드 변경 없음)

- `scripts/_lang_check.py`(새 파일): 언어 검사와 요약 표. 병기 인정 범위(2-1)와 LE03 보조 지표(2-5)는 결정(2026-11-19)을 반영해 기준선 전에 고쳤다.
- `scripts/evaluate_chat.py`: `--language`, `--question-set lang`, 행·조건에 `language`, 모든 행에 `languageCheck`, 요약 .md에 언어 절.
- `scripts/compare_chat_versions.py`: 답변 언어 절(설정 × 사용자 언어). 언어가 어긋난 회차를 실패 사례에 `language`로 적는다.
  회귀 판정 항목에는 넣지 않는다. #106 전 측정 행은 답변으로 다시 계산한다.
- `specs/chat/answer_language_questions_v1.json`, `answer_language_allowlist_v1.json`, `variants_lang_baseline_v1.json`, `variants_lang_v1.json`.
- 프롬프트·도구 출력·서버 설정은 바꾸지 않았다.

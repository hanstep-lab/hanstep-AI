# chat.done `blocks` 형식 v1 (프론트 계약)

- 2026-11-19 조사 문서(`_local/chat-blocks-format.md`, 저장소 밖)를 옮겨 왔다(#103). 그 뒤 바뀐 것을 반영했다.
  - `MISSING_INPUTS.question`이 영어로 바뀌었다(#103).
  - `FX_SUMMARY_CARD`가 생겼다(#100).
  - 운영 프롬프트가 v4.4가 됐다(hanstep-backend #144).
  - 프론트가 blocks를 그린다(hanstep-frontend #34 등).
- 기준 커밋
  - hanstep-AI `origin/develop` `d465761`(Merge #102) + 이 문서를 넣은 #103(`ai/app/visa/tools.py`, `ai/app/visa/e74/`)
  - hanstep-backend `origin/develop` `f6f3623`(Merge #144)
  - hanstep-frontend `origin/develop` `4fb82ba`(#36)
- 경로는 각 저장소 루트 기준이다. AI 코드는 `ai/` 아래.

## 0. 요약

| type | 만들어지는 조건 | 필드(타입·null) | 프론트(develop) |
|---|---|---|---|
| `E74_SCORE_CARD` | `calculate_e74_score` SUCCEEDED. 프롬프트 v2 이상. Spring이 설정돼 있어야 한다(fake 모드에서는 도구가 FAILED라 안 나옴) | `assessmentId` int · `status` enum · `total` int · `potentialTotal` int · `required` int\|null · `ruleVersion` string\|null · `ruleStatus` enum\|null · `isScenario` bool | `E74ScoreCard` |
| `MISSING_INPUTS` | 같은 E-7-4 결과에 `missingInputs`가 1개 이상이면 `E74_SCORE_CARD` 바로 뒤 | `fields`(1개 이상) `{field: string, question: string}`. `question`은 사용자 언어(ko 한국어, en·그 밖 영어. #108). null 없음 | `MissingInputsCard`(`question` 그대로, 3개까지) |
| `REMITTANCE_CONFIRM` | `quote_remittance` SUCCEEDED이고 견적이 요청과 맞을 때. 기본 상품이 보낼 수 없는 금액이면 없다. 프롬프트 v3 이상. fake 모드에서도 fixture로 나온다 | `recipient`{…} · `sendAmount` · `receiveAmount`\|null · `appliedRate`{…}\|null · `transferPath` · `fees`{…} · `rateSources`{…}(#105, 코드·숫자만) | `RemittanceConfirmCard`(`fees`·`rateSources`는 아직 안 씀) |
| `FX_SUMMARY_CARD` | `fx_rate_summary` SUCCEEDED이고 통화가 1개 이상. 프롬프트 v4.4만. 한 턴에 1개(통화별로 합침) | `currencies`(1개 이상) · `unavailableCurrencies`. 3.5절 | `FxSummaryCard` |
| `VISA_ROADMAP_LINK` | **채팅에서 안 나온다.** 형식만 있다(`get_visa_roadmap`이 채팅 도구가 아님) | `roadmapId` int · `stepCount` int · `applicableFrom` date\|null · `ruleStatus` enum\|null | 모르는 type이라 버린다 |

- 실서버 답변에는 추천 질문 칩(`suggestions`)이 없다. 칩은 앱이 정한다(6절).

---

## 1. blocks가 만들어지는 경로 (AI → Spring → 앱)

```
도구 실행 결과 ToolOutcome.blocks
  → 그래프 tools 노드가 {"type":"tool_call", ..., "blocks": [...]} 를 스트림에 씀
  → stream.py 가 턴 전체의 blocks 를 모음(같은 내용은 한 번만, FX 카드는 하나로 합침)
  → agent.done {"content", "blocks": [...] 또는 null, ...}           (AI → Spring, INT-05)
  → Spring ChatStreamService.done(): blocks JsonNode 를 그대로
       ├ chat_messages.blocks_json (MySQL JSON) 에 저장
       └ chat.done {"runId","assistantMessageId","blocks","latencyMs"}  (Spring → 앱, CHT-04)
  → 대화방을 다시 열면 CHT-03 messages[].blocks 로 같은 값이 온다(앱은 아직 CHT-03을 부르지 않음)
```

| 단계 | 위치 |
|---|---|
| 도구 결과에 blocks 필드 | hanstep-AI `ai/app/chat/tools.py:88`(`ToolOutcome.blocks`, 기본 빈 목록) |
| 그래프가 tool_call 항목에 blocks를 실음 | `ai/app/chat/graph.py:133` |
| 턴 전체 blocks 모음·중복 제거·FX 카드 합치기 | `ai/app/chat/stream.py:141`, `:231-235`(`merge_fx_card`) |
| agent.done에 `blocks or None` | `ai/app/chat/stream.py:272` |
| E-7-4 카드·재질의 카드 생성 | `ai/app/visa/tools.py:243-280`(모델), `_missing_block`·`_question_en`, `e74_records` |
| 채팅 비자 도구 → blocks 전달 | `ai/app/chat/visa_tools.py:265-282`(`_succeeded`) |
| 송금 확인 카드 생성 | `ai/app/chat/remittance_tools.py` `RemittanceConfirmBlock`·`confirm_block`, `RemittanceTools._quote`, `_quote_matches`(#105에서 줄이 바뀌어 이름으로 적음) |
| 환율 진단 카드 생성·합치기 | `ai/app/chat/fx_tools.py:188-276`(모델·`summary_block`·`merge_fx_card`), `:336` |
| 로드맵 카드(채팅 미연결) | `ai/app/visa/tools.py:257-264`, `roadmap_records`. 채팅 도구 목록 `ai/app/chat/visa_tools.py:54`에 `get_visa_roadmap` 없음 |
| 프롬프트 버전별 도구 연결 | `ai/app/api/routes/chat.py:60-74`, `:227-278` |
| Spring 중계 | hanstep-backend `src/main/java/com/hanstep/chat/service/ChatStreamService.java:316-330` |

blocks를 만드는 도구는 `calculate_e74_score`, `quote_remittance`, `fx_rate_summary` 세 개다.
`get_financial_analysis`, `search_visa_policies`, `get_remittance_recipients`, `get_fx_rate`, 상품 추천, 금융 정책 검색(`search_finance_policy`)은 blocks를 만들지 않는다.

## 2. 공통 규칙

### 2.1 값의 모양
- `blocks`는 **null 또는 1개 이상의 배열**이다. 빈 배열 `[]`은 오지 않는다(`stream.py:272` `blocks or None`. Spring도 null은 null로 둔다).
- 블록마다 `type`(대문자 스네이크) 문자열이 있다. 필드 이름은 camelCase다.
- enum은 문자열로 온다.
- 블록의 숫자는 **도구 결과에서만** 채운다(LLM이 쓴 숫자가 들어가지 않음). `ai/app/visa/tools.py:11`, `remittance_tools.py:6`, `fx_tools.py:15`.
- 한 턴의 blocks에는 그 턴에 실행된 도구의 카드만 들어간다(이전 턴 카드는 다시 오지 않음).
- 답변이 실패하면(`chat.error`) blocks가 없다. FAILED·PARTIAL 메시지의 `blocks_json`은 null로 저장된다(`ChatStreamService.java:344`, `:352`).

### 2.2 개수·순서
- 순서: **도구 실행 순서**(`chat.tool_call`의 `order` 순서)다. E-7-4 한 번의 결과는 `[E74_SCORE_CARD, MISSING_INPUTS?]` 순서다.
- 내용이 완전히 같은 블록은 하나만 남는다(`stream.py:234`).
- `FX_SUMMARY_CARD`는 한 턴에 1개다. 여러 번 불러도 처음 나온 자리에 통화별로 합친다(같은 통화는 나중 값, `fx_tools.py:250-276`).
- 개수 상한을 따로 두는 코드는 없다. 도구 호출 상한 `CHAT_MAX_TOOL_CALLS`(기본 3, `ai/app/core/config.py:125`. 운영 compose에는 없음 → 3)이 사실상의 상한이다.
  - 다만 LLM이 한 번에 여러 도구를 같이 부르면 그 묶음은 모두 실행되므로(`graph.py`의 `route`는 묶음 전에만 상한을 확인) 3을 넘을 수 있다.
- 보통은 0~2개다.
  - 점수 질문: `[E74_SCORE_CARD]` 또는 `[E74_SCORE_CARD, MISSING_INPUTS]`
  - 실제 점수와 가정 점수를 같이 물으면: `E74_SCORE_CARD` 2개(`isScenario`로 구분)
  - 송금 견적: `[REMITTANCE_CONFIRM]`. 받는 분·통화가 애매하면 텍스트로 되묻고 blocks는 null
  - 환율 질문(v4.4): `[FX_SUMMARY_CARD]`
  - 같은 턴에 여러 종류가 섞일 수도 있다

### 2.3 어떤 설정에서 켜지나

| 프롬프트 버전 | `E74_SCORE_CARD`·`MISSING_INPUTS` | `REMITTANCE_CONFIRM` | `FX_SUMMARY_CARD` | 비고 |
|---|---|---|---|---|
| v1, v1.1 | – | – | – | 금융 분석 도구만 |
| v2 | O | – | – | |
| v3, v3.1, v3.2 | O | O | – | |
| v4, v4.1, v4.2, v4.3a | O | O | – | 상품 추천(v4~)·금융 정책 검색(v4.2~)이 더 붙지만 blocks 없음 |
| v4.4 | O | O | O | 환율 진단 `fx_rate_summary`가 더 붙는다 |

- 버전 설정: `CHAT_PROMPT_VERSION`. AI 코드 기본값은 v4.1(`ai/app/core/config.py:136`)이다.
- 운영은 compose 기본값 **v4.4**다(hanstep-backend `deploy/compose.prod.yml:151`, #144, 2026-11-19 머지). 서버 `deploy/.env`에는 이 값을 두지 않는다. → **운영에서는 네 종류(`E74_SCORE_CARD`, `MISSING_INPUTS`, `REMITTANCE_CONFIRM`, `FX_SUMMARY_CARD`)가 모두 나올 수 있다.**
- 데이터 출처
  - `CHAT_LLM_MODE=anthropic` + Spring 내부 API 설정: 점수는 Spring이 계산·저장한 결과, 송금은 Spring 견적, 환율은 Spring fx-summary
  - fake 모드이거나 Spring 설정이 없을 때: E-7-4 소스가 없어 도구가 `SPRING_NOT_CONFIGURED`로 FAILED → **점수 카드 없음**(`ai/app/api/routes/chat.py`). 송금은 fixture(LORNA REYES, 1 PHP = 22.32 KRW)로 카드가 나온다(`remittance_tools.py`)

---

## 3. 블록 종류별 상세

### 3.1 `E74_SCORE_CARD` — E-7-4 점수 카드

- 만들어지는 곳: `ai/app/visa/tools.py` `e74_records`, 모델 `:243-255`
- 조건: `calculate_e74_score` SUCCEEDED. Spring 4xx·5xx·시간 초과·결과 형식 오류면 FAILED·REJECTED·TIMEOUT이고 카드가 없다.
- 자세한 값(항목별 점수, 개선 방안 등)은 카드에 없다. `assessmentId`로 `e74_assessments.result_json`을 읽는다(Spring 비자 API).

| 필드 | 타입 | null | 뜻 |
|---|---|---|---|
| `type` | `"E74_SCORE_CARD"` | 아니오 | |
| `assessmentId` | int (≥1) | 아니오 | `e74_assessments.id`(Spring이 저장한 행) |
| `status` | `ELIGIBLE` \| `INELIGIBLE` \| `NEEDS_EVIDENCE` \| `NEEDS_REVIEW` | 아니오 | 사전진단 결과(허가가 아님). **점수와 요건을 함께 본 판정**이라 `total ≥ required`여도 요건 미충족이면 `INELIGIBLE`일 수 있다(예: Reyes는 235점 ≥ 200점이지만 체류 기간 요건 `REQ_STAY_PERIOD` NOT_MET → INELIGIBLE) |
| `total` | int | 아니오 | 확정 총점(증빙 확인된 값만). 단위: 점 |
| `potentialTotal` | int | 아니오 | 신고값까지 인정한 총점. 단위: 점 |
| `required` | int | **예** | 적용 기준 총점(보통 200, 한국어 유예 시 150). 적용할 규칙 버전이 없으면 null |
| `ruleVersion` | string(예: `E74-2026-v1`) | **예** | 계산에 쓴 규칙 버전. 적용할 규칙 버전이 없으면 null |
| `ruleStatus` | `DRAFT` \| `APPROVED` \| `RETIRED` | **예** | 그 규칙 버전의 승인 상태 |
| `isScenario` | bool | 아니오 | true면 가정 계산(기준일이 오늘이 아니거나 증빙 취득·소득 변경 같은 가정 조건). 화면에 "가정" 표시가 필요하다 |

- 숫자: Spring이 돌려준 계산 결과(`result.score.total`, `result.potentialScore.total`, `result.score.required`) 그대로다. 단위는 **점**이다.
- `required`·`ruleVersion`이 null인 경우: 적용할 승인 규칙 버전이 없을 때 계산 엔진이 `status=NEEDS_REVIEW`, `total=0`으로 돌려준다. 운영에서는 Spring이 이 경우를 오류(`NO_APPLICABLE_RULE_SET`)로 돌려줄 수 있어 카드에 null이 오는 일은 드물다. 타입은 null 가능으로 받아야 한다.

### 3.2 `MISSING_INPUTS` — 추가 입력 요청 카드

- 만들어지는 곳: `ai/app/visa/tools.py:267-280`(모델), `_missing_block`·`_question_en`
- 조건: E-7-4 결과의 `missingInputs`가 1개 이상이면 `E74_SCORE_CARD` 바로 뒤에 하나 붙는다. 없으면 붙지 않는다.

| 필드 | 타입 | null | 뜻 |
|---|---|---|---|
| `type` | `"MISSING_INPUTS"` | 아니오 | |
| `fields` | 배열(1개 이상) | 아니오 | 판정에 필요한데 없는 입력 |
| `fields[].field` | string | 아니오 | 입력 경로(예: `incomeRecords[incomeYear=2024]`, `legalDeclaration.finesUnder1mCount`, `visaProfile.currentStayStartedOn`). 화면 이동·입력 칸 연결에 쓸 수 있는 키 |
| `fields[].question` | string | 아니오 | 물어볼 내용. 채팅 요청의 사용자 언어 문구다(#108): ko면 한국어(엔진 `question`), en이면 영어 한 문장(엔진 `questionEn`, #103). vi·zh는 영어 |

- 모양(`{field, question}`)은 #103 전과 같다. `question`의 언어만 사용자 언어를 따른다(#103에서는 영어 고정, #108부터 ko는 한국어).
- en: 엔진 결과의 `questionEn`이다. #103 전에 저장돼 재사용된 평가·로드맵(`questionEn` 없음)은 한국어 문구로 같은 질문의 영어 문구를 찾아 넣는다. 문구 목록에 없으면 한국어 그대로다. ko: 엔진 결과의 `question`(한국어) 그대로다.
- 숫자 필드 없음.
- 감점·제외 6문항(`legalDeclaration.*`)은 저장하지 않는 자기신고 항목이라, 프로필이 비어 있으면 거의 항상 목록에 들어간다.
- LLM에 주는 도구 결과의 `missingInputs`는 #101에서 잰 모양 그대로(`{field, question, ruleCodes}`)다. #108부터 `question`은 사용자 언어 문구(ko 한국어, en 카드와 같은 영어)다. 3.2.1절 참고.

#### E-7-4 결과·로드맵 응답의 `missingInputs` (채팅 카드가 아닌 쪽)

Spring 비자 API(E-7-4 평가 `result`, 로드맵 `roadmap`)는 AI 엔진 결과를 JsonNode로 그대로 준다. 그 안의 `missingInputs` 항목은 채팅 카드와 모양이 다르다.

| 필드 | 타입 | null | 뜻 |
|---|---|---|---|
| `field` | string | 아니오 | 채팅 카드와 같다 |
| `question` | string | 아니오 | **한국어**(바뀌지 않음) |
| `questionEn` | string | **예**(#103 전 저장분은 키 없음) | 같은 뜻의 영어(#103) |
| `ruleCodes` | string[] | 아니오 | 이 입력이 필요한 규칙 코드(전체 `missingInputs`에만. 항목별 `items[].missingInputs`에는 없음) |

- 로드맵 화면(VS-001) 규칙(**임시**): 앱 언어가 `ko`면 `question`, 그 밖이면 `questionEn`. `questionEn`이 없으면 `question`.
- 문구는 `ai/app/visa/e74/questions.py`의 `QUESTIONS`에 질문 키별로 한곳에 있다(27개, 감점·제외 6개 포함. 근무지 인구감소지역 질문은 근무 중·종료로 둘).

#### 3.2.1 E-7-4 도구가 LLM에 주는 값 (#108, 카드 아님)

`calculate_e74_score`의 도구 결과(`llmOutput`)는 채팅 요청의 사용자 언어(`language`, Spring이 `users.preferred_language`로 채움)로 고른다. ko면 한국어, en이면 영어다. vi·zh는 고려하지 않으며 영어 결과를 받는다(측정하지 않음). 도구 정의(설명·인자 설명)는 LLM용이라 영어 하나다. 엔진 결과의 한국어 필드는 그대로 두고(로드맵 화면·저장용), LLM에만 같은 key의 문구를 준다. 문구는 `ai/app/visa/e74/texts.py` 한곳에 key별로 있다.

| LLM 결과 경로 | ko | en | 엔진 결과(Spring `result_json`) |
|---|---|---|---|
| `missingInputs[].question` | `question`(한국어) | `questionEn`(카드와 같음) | `question`(한국어)·`questionEn` 그대로 |
| `improvements[].description` | `description`에서 내부 표기를 뺀 한국어 | `descriptionEn` | `description`(한국어) 그대로 + `descriptionEn`(#108, 전 저장분은 키 없음) |
| `improvements[].documents` | `documents`에서 내부 표기를 뺀 한국어 | `documentsEn` | `documents`(한국어) 그대로 + `documentsEn`(#108, 전 저장분은 키 없음) |
| `reviewNotices[]` `{code, ruleCodes, text}` | 검토 메모 key와 한국어 안내 | 검토 메모 key와 영어 안내 | `reviewNotes` 그대로 |

- 두 언어 모두 `reviewNotes`(사람 확인용 한국어 메모)는 LLM에 주지 않는다.
- 두 언어 모두 내부 표기도 주지 않는다. 확인 번호(Q8·Q9), 양식 번호("(붙임5)"·"(붙임9 견본)"), "검토 필요"가 해당한다. ko 문구는 `texts.py`의 `llm_ko`다(예: "고용기업 추천서(공식 양식)").
- en: `descriptionEn`·`documentsEn`·`questionEn`이 없는 재사용 평가(#108·#103 전 저장분)는 한국어 문구로 같은 문구의 영어를 찾는다. 찾지 못한 개선 방안의 `description`·`documents`는 한국어 대신 null이다.
- `reviewNotices[].code`: `NO_APPROVED_RULES`, `AGE_OUTSIDE_SCORE_TABLE`, `INCOME_FROM_EARLIER_YEARS`, `KOREAN_LEVEL_UNREADABLE`, `CERTIFICATE_GRADE_NOT_LISTED`, `STAY_SPECIAL_RULE_CHECK`, `REQUIRED_TOTAL_UNKNOWN`. 그 밖의 메모는 `MANUAL_CHECK`다. 같은 안내는 하나로 합친다(소득 대체 연도 메모는 소득 점수·최소 소득 요건 두 항목).
- 사용자 입력 원문(예: 자격증 이름 `'용접기능사'`)은 영어 문구 안에 그대로 들어갈 수 있다.
- 점수 카드(`E74_SCORE_CARD`) 숫자는 언어와 상관없다. `MISSING_INPUTS.question`은 LLM 결과와 같은 언어다(3.2절).

### 3.3 `REMITTANCE_CONFIRM` — 송금 확인 카드

- 만들어지는 곳: `ai/app/chat/remittance_tools.py` `RemittanceConfirmBlock`·`confirm_block`, `RemittanceTools._quote`. 계약 문서 `ai/specs/chat/remittance_contract_v1.md` "확인 카드(chat.done blocks)" 절
- 조건: `quote_remittance` SUCCEEDED. 다음이면 카드가 없다.
  - 받는 분이 사용자의 저장된 받는 분이 아님(REJECTED `UNKNOWN_RECIPIENT`)
  - 통화가 KRW나 받는 분 통화가 아님(REJECTED `UNSUPPORTED_CURRENCY`)
  - Spring 견적의 받는 분·통화·금액이 요청과 다름(FAILED `INVALID_RESPONSE`, `_quote_matches`)
  - 기본 상품(`defaultProduct`, 지금은 쏠편한)이 보낼 수 없는 금액(REJECTED `QUOTE_UNAVAILABLE`, 예: 4,700원 `AMOUNT_TOO_SMALL`)
  - Spring 오류·시간 초과
- 송금을 실행하지 않는다. 사용자는 카드에서 송금 화면으로 가서 확인하고 보낸다.
- **카드에는 코드·숫자만 넣는다(#105).** 제목·항목 이름·안내 문구·출처 문구는 앱이 화면 언어로 만든다. 한국어로만 있는 출처 제목·심사필 번호는 카드에 없다(LLM 입력에만 있다).

| 필드 | 타입 | null | 뜻 |
|---|---|---|---|
| `type` | `"REMITTANCE_CONFIRM"` | 아니오 | |
| `recipient.recipientId` | int (≥1) | 아니오 | 받는 분 id |
| `recipient.nameEn` | string (1~100자) | 아니오 | 받는 분 영문 이름 |
| `recipient.relationship` | string (1~30자, 예: `MOTHER`, `SELF`) | 아니오 | 관계 **코드**. 화면 문구("어머니")는 앱이 정한다 |
| `recipient.countryCode` | string (2자, 예: `PH`) | 아니오 | |
| `recipient.currency` | string (3자, 예: `PHP`) | 아니오 | 받는 분 통화 |
| `recipient.bankName` | string (1~100자) | 아니오 | |
| `recipient.accountLast4` | string (숫자 4자리) | 아니오 | 계좌 끝 4자리(문자열) |
| `sendAmount.amount` | int (≥0) | 아니오 | 보낼 금액. 단위: **원**(KRW 정수) |
| `sendAmount.currency` | `"KRW"` | 아니오 | 항상 KRW |
| `receiveAmount` | 객체 | **예**(#105) | PHP 참고 금액. PHP 참고 환율이 없을 때만 null |
| `receiveAmount.amount` | int (≥0) | 아니오 | 받을 금액(**참고값**, 현지 환율로 바뀜). 단위: **받는 분 통화의 정수**(소수 없음, 예: 23587 PHP) |
| `receiveAmount.currency` | string (3자) | 아니오 | `recipient.currency`와 같음 |
| `appliedRate` | 객체 | **예**(#105) | PHP **참고** 환율(송금 환율 아님). PHP 참고 환율이 없을 때만 null |
| `appliedRate.currency` | string (3자) | 아니오 | |
| `appliedRate.unit` | int (≥1) | 아니오 | `unit` 외화 = `rate` 원. 보통 1, VND 같은 통화는 100일 수 있음 |
| `appliedRate.rate` | number (>0, 소수) | 아니오 | `unit` 외화당 원화. 예: 22.32 → "1 PHP = 22.32 KRW" |
| `appliedRate.sourceLabel` | string (1~100자) | 아니오 | 환율 출처 문구(예: `ECB 기준환율 기반 참고값`). Spring이 준 한국어 문자열. 하위 호환으로 둔다. **앱은 이 값 대신 `rateSources`로 문구를 만든다** |
| `appliedRate.asOf` | date `YYYY-MM-DD` | 아니오 | 환율 기준일 |
| `appliedRate.isMock` | bool | 아니오 | true면 참고·데모 값. LLM에는 주지 않고 카드에만 실린다 |
| `transferPath` | string | 아니오 | 송금 화면 경로 `/transfer?recipientId={id}&amount={sendAmount.amount}`. `amount`는 보낼 원화 금액 |
| `fees` | 객체 | 예(fake 모드만) | 기본 상품 수수료·환율(#105). Spring 값 그대로 |
| `fees.product` | `SOL_EASY` \| `SOL_FAST` | 아니오 | 상품 코드 |
| `fees.sendFeeKrw` | int | 예(쏠빠른) | 송금수수료(원). 쏠편한은 USD 5,000 이하 0원 |
| `fees.telegraphicFeeKrw` | int | 예(쏠빠른) | 전신료(원) |
| `fees.feeUsd` | number | 예(쏠편한) | 송금수수료(USD). 쏠빠른 3.5 |
| `fees.fxMarginKrw` | int | 아니오 | 환전 마진(원). 송금 환율에 이미 들어 있어 출금 총액에 더하지 않는다 |
| `fees.totalWithdrawKrw` | int | 아니오 | 출금 총액(원). `sendAmount.amount`를 넘지 않는다 |
| `fees.preferentialPct` | number | 아니오 | 환율 우대율(%). 쏠빠른 0 |
| `fees.usdAppliedRate` | number | 아니오 | **송금 환율**(1 USD 당 원). 송금은 USD로 나간다 |
| `fees.notices` | string[] | 아니오 | 안내 코드. 목록은 `remittance_contract_v1.md` 확인 카드 절 |
| `rateSources` | 객체 | 예(fake 모드만) | 환율 출처 코드(#105). 앱이 코드로 문구를 고른다 |
| `rateSources.usd` | string(`KOREAEXIM`) | 아니오 | USD 매매기준율 출처 |
| `rateSources.php` | string(`frankfurter-ecb`, 로컬 Mock만 `MOCK`) | 예 | PHP 참고 환율 출처. PHP 참고 환율이 없으면 null |

- 숫자: 견적 결과 그대로다. 운영에서는 Spring이 계산하고 AI는 요청과 맞는지만 확인한다. fake 모드는 fixture 규칙(보낼 KRW → 받을 금액 내림, 받을 금액 → 보낼 KRW 올림)으로 계산한다.
- LLM에 주는 `rateText`("1 PHP = 22.32 KRW", "1 USD = 1,334.91 KRW")는 카드에 없다. 화면에서 `unit`·`currency`·`rate`, `fees.usdAppliedRate`로 만든다.
- 화면 용어는 TR-001 송금 화면(FE #33)과 맞춘다: 받는 금액은 참고(reference), 송금 환율은 1 USD = … KRW, PHP 환율은 참고 환율.
- fake 모드(fixture 견적)는 상품 견적이 없어 `fees`·`rateSources`가 null이다. 운영(Spring)에서는 늘 있다.
- 개인정보: 받는 분 영문 이름·은행·계좌 끝 4자리가 카드와 `blocks_json`에 들어간다(전체 계좌번호는 없음).

### 3.4 `VISA_ROADMAP_LINK` — 로드맵 화면 이동 카드 (채팅에서 안 나옴)

- 모델: `ai/app/visa/tools.py:257-264`, 생성 `roadmap_records`. 단위 테스트(`ai/tests/unit/test_visa_tools.py`)에서만 쓴다.
- 채팅 비자 도구 목록이 `(search_visa_policies, calculate_e74_score)`뿐이라(`ai/app/chat/visa_tools.py:54`) chat.done에 오지 않는다.
- 필드: `roadmapId` int(≥1, `visa_roadmaps.id`) · `stepCount` int(≥0, 단계 수. 단위: 개) · `applicableFrom` date\|null(신청 가능일, 판정할 수 없으면 null) · `ruleStatus` `DRAFT|APPROVED|RETIRED`\|null. 로드맵에 누락 입력이 있으면 뒤에 `MISSING_INPUTS`가 붙는 구조다.
- 프론트는 이 type을 모른다(`KNOWN_BLOCK_TYPES`에 없음). 채팅에 연결하면 프론트도 함께 바꿔야 한다.

### 3.5 `FX_SUMMARY_CARD` — 환율 진단 카드

- 만들어지는 곳: `ai/app/chat/fx_tools.py:188-276`(모델 `FxSummaryCardBlock`·`FxCurrencyCard`·`FxPeriodAverage`, `summary_block`, `merge_fx_card`), `:336`
- 조건: 프롬프트 v4.4에서 `fx_rate_summary`가 SUCCEEDED이고 통화가 1개 이상(`fx_tools.py:232-236`). 실패(FAILED·REJECTED·TIMEOUT·INVALID_RESPONSE)이면 카드가 없다.
- 한 턴에 1개다(2.2절). 문장·버튼 경로는 넣지 않는다. 버튼(환율 상세, 환율 알림, 송금하기)은 앱이 고정으로 붙인다.
- 필드 이름은 환율 화면 API(FX-001)와 같다. 값은 Spring fx-summary 원본 그대로이고, 평균을 내지 못한 기간(`avg` null)만 뺀다.

| 필드 | 타입 | null | 뜻 |
|---|---|---|---|
| `type` | `"FX_SUMMARY_CARD"` | 아니오 | |
| `currencies` | 배열(1개 이상) | 아니오 | 통화별 값. 순서는 처음 나온 순서 |
| `currencies[].currency` | string (3자, 예: `USD`, `PHP`) | 아니오 | |
| `currencies[].unit` | int (≥1) | 아니오 | `unit` 외화 = `rate` 원 |
| `currencies[].rate` | number (>0) | 아니오 | 기준 환율. 단위: 원 |
| `currencies[].sendRate` | number | **예** | 송금 보낼 때 환율. `KOREAEXIM`만 있고 그 밖에는 null |
| `currencies[].receiveRate` | number | **예** | 송금 받을 때 환율. `KOREAEXIM`만 있고 그 밖에는 null |
| `currencies[].change` | number(부호 있음) | **예** | 전 영업일 대비(원). 전 영업일 값이 없으면 null |
| `currencies[].changeRate` | number(부호 있음) | **예** | 전 영업일 대비(%). 없으면 null |
| `currencies[].source` | string (1~30자, 예: `KOREAEXIM`, `MARKET_REF`) | 아니오 | 원천 코드. 화면 문구는 앱이 정한다(`MARKET_REF`는 ECB 기반 시장 참고값, 송금 환율 아님) |
| `currencies[].asOf` | date `YYYY-MM-DD` | 아니오 | 환율 기준일 |
| `currencies[].fetchedAt` | datetime(오프셋 포함) | 아니오 | 마지막 확인 시각 |
| `currencies[].periodAverages` | 배열(0~4개) | 아니오 | 기간 평균. 데이터가 부족한 기간은 빠진다 |
| `currencies[].periodAverages[].period` | `1M` \| `3M` \| `6M` \| `1Y` | 아니오 | |
| `currencies[].periodAverages[].avg` | number (>0) | 아니오 | 기간 평균(원) |
| `currencies[].periodAverages[].diff` | number(부호 있음) | 아니오 | 현재 − 평균(원) |
| `currencies[].periodAverages[].diffRate` | number(부호 있음) | 아니오 | (현재 − 평균) ÷ 평균 × 100(%) |
| `currencies[].periodAverages[].direction` | `LOWER` \| `HIGHER` \| `SAME` | 아니오 | 현재가 평균보다 낮은지·높은지 |
| `currencies[].periodAverages[].businessDays` | int (≥1) | 아니오 | 평균을 낸 영업일 수. 단위: 일 |
| `currencies[].isMock` | bool | 아니오 | true면 데모 데이터 |
| `currencies[].stale` | bool | 아니오 | true면 오래된 값(기준일·마지막 확인 시각을 강조) |
| `unavailableCurrencies` | string[] | 아니오 | 값을 받지 못한 통화(카드에 실제로 있는 통화는 빠진다) |

---

## 4. 실제 예시 JSON

만든 방법: 4.1·4.2·4.4는 hanstep-AI 가상환경에서 `e74_records`·`roadmap_records`를 직접 실행했다(#103 브랜치). 4.3은 INT-05 라우트를 fake 모드로 불렀다. 4.5는 #101 측정(로컬 Spring, 실데이터 백필)의 v4.4 답변에서 꺼냈다. 값은 Reyes 데모·합성 프로필·fixture·로컬 측정뿐이다.

### 4.1 `E74_SCORE_CARD`

Reyes 데모 프로필(`ai/tests/fixtures/e74/profiles/P05_reyes.json`)을 기준일 2026-11-19로 계산해 `e74_records`에 넣은 결과. `assessmentId`는 임의 값 1(실제는 Spring이 저장한 행 id).

```json
{
  "type": "E74_SCORE_CARD",
  "assessmentId": 1,
  "status": "INELIGIBLE",
  "total": 235,
  "potentialTotal": 285,
  "required": 200,
  "ruleVersion": "E74-2026-v1",
  "ruleStatus": "APPROVED",
  "isScenario": false
}
```

### 4.2 `MISSING_INPUTS`

최소 입력 합성 프로필(`P02_minimal.json`, 가상 인물)을 기준일 2026-11-19로 계산한 결과(11개 항목 전부). 이 블록 앞에는 `E74_SCORE_CARD`(`status` `NEEDS_EVIDENCE`, `total` 0)가 온다.

```json
{
  "type": "MISSING_INPUTS",
  "fields": [
    {"field": "incomeRecords[incomeYear=2024]", "question": "What was your annual income in 2024 (the amount on your income certificate from the tax office)?"},
    {"field": "incomeRecords[incomeYear=2025]", "question": "What was your annual income in 2025 (the amount on your income certificate from the tax office)?"},
    {"field": "visaEvidences[evidenceType=TOPIK|KIIP|KIIP_PRE_TEST]", "question": "What is your Korean level (TOPIK level, completed KIIP stage or KIIP pre-test score)?"},
    {"field": "employmentHistories", "question": "What is your work history (workplace, industry, start date and end date)?"},
    {"field": "legalDeclaration.finesUnder1mCount", "question": "In the last 10 years, how many times did you receive a fine under KRW 1,000,000?"},
    {"field": "legalDeclaration.taxStayRestrictionCount", "question": "In the last 10 years, how many times was your stay permission limited because of unpaid taxes?"},
    {"field": "legalDeclaration.immigrationViolationCount", "question": "In the last 10 years, how many times did you receive a penalty under the Immigration Act, including administrative fines?"},
    {"field": "visaProfile.currentStayStartedOn", "question": "When did your current stay start (the date you last entered Korea)?"},
    {"field": "legalDeclaration.hasFine1mOrMore", "question": "In the last 10 years, did you receive a fine of KRW 1,000,000 or more?"},
    {"field": "legalDeclaration.hasCurrentTaxArrears", "question": "Do you have any unpaid taxes right now?"},
    {"field": "legalDeclaration.hasOverstay3mOrMore", "question": "In the last 10 years, did you stay in Korea for 3 months or more after your permitted stay ended?"}
  ]
}
```

같은 결과의 엔진 `missingInputs` 항목 하나(Spring 비자 API `result.missingInputs`에 이 모양으로 온다):

```json
{"field": "incomeRecords[incomeYear=2024]", "question": "2024년 연간 소득(세무서 소득금액증명원 기준 금액)", "questionEn": "What was your annual income in 2024 (the amount on your income certificate from the tax office)?", "ruleCodes": ["BASE_INCOME", "REQ_MIN_INCOME"]}
```

### 4.3 `REMITTANCE_CONFIRM`

운영 실제 견적(Reyes, 500,000원, 2026-11-19, `tests/fixtures/chat/remittance_quote_reyes_500000.json`)으로 만든 카드(#105):

```json
{
  "type": "REMITTANCE_CONFIRM",
  "recipient": {"recipientId": 1, "nameEn": "LORNA REYES", "relationship": "MOTHER", "countryCode": "PH", "currency": "PHP", "bankName": "BDO", "accountLast4": "5566"},
  "sendAmount": {"amount": 500000, "currency": "KRW"},
  "receiveAmount": {"amount": 23587, "currency": "PHP"},
  "appliedRate": {"currency": "PHP", "unit": 1, "rate": 20.926, "sourceLabel": "ECB 기준환율 기반 참고값", "asOf": "2026-11-18", "isMock": false},
  "transferPath": "/transfer?recipientId=1&amount=500000",
  "fees": {"product": "SOL_EASY", "sendFeeKrw": 0, "telegraphicFeeKrw": 5000, "feeUsd": null, "fxMarginKrw": 1413, "totalWithdrawKrw": 499998, "preferentialPct": 70, "usdAppliedRate": 1334.91, "notices": ["INTERMEDIARY_FEE_MAY_APPLY", "LOCAL_PAYOUT_RATE_DIFFERS", "FOREIGN_EXCHANGE_BANK_DESIGNATION_REQUIRED"]},
  "rateSources": {"usd": "KOREAEXIM", "php": "frankfurter-ecb"}
}
```

아래는 #105 전에 INT-05 라우트를 fake 모드로 호출한 `agent.done` 원문이다(`CHAT_PROMPT_VERSION=v4.1`, 질문 "send 500k won to mama pls", 송금 fixture 값). #105 뒤 fake 모드 카드에는 `"fees": null, "rateSources": null`이 더 붙는다.

```
event: agent.done
data: {"content":"LORNA REYES will receive 22,401 PHP for 500,000 KRW. Please check the confirmation card and send it on the transfer screen.","blocks":[{"type":"REMITTANCE_CONFIRM","recipient":{"recipientId":11,"nameEn":"LORNA REYES","relationship":"MOTHER","countryCode":"PH","currency":"PHP","bankName":"BDO","accountLast4":"5566"},"sendAmount":{"amount":500000,"currency":"KRW"},"receiveAmount":{"amount":22401,"currency":"PHP"},"appliedRate":{"currency":"PHP","unit":1,"rate":22.32,"sourceLabel":"ECB 기준환율 기반 참고값","asOf":"2026-11-06","isMock":true},"transferPath":"/transfer?recipientId=11&amount=500000"}],"modelId":"fake-chat-model","promptVersion":"chat-v4.1","latencyMs":146,"usage":{"inputTokens":0,"outputTokens":0,"cacheReadInputTokens":0,"cacheCreationInputTokens":0,"llmCalls":3},"timings":{"firstDeltaMs":88,"toolMs":1,"totalMs":146}}
```

Spring이 앱에 보내는 `chat.done`(Spring 코드 `ChatStreamService.java:328`, `ChatStreamEvents.Done`으로 정리한 모양. `runId`·`assistantMessageId`·`latencyMs` 값은 예시):

```
event: chat.done
data: {"runId":"run_4f1c2b7e9d0a4c3b8e6f5a1d2c3b4a59","assistantMessageId":11,"blocks":[{"type":"REMITTANCE_CONFIRM","recipient":{"recipientId":11,"nameEn":"LORNA REYES","relationship":"MOTHER","countryCode":"PH","currency":"PHP","bankName":"BDO","accountLast4":"5566"},"sendAmount":{"amount":500000,"currency":"KRW"},"receiveAmount":{"amount":22401,"currency":"PHP"},"appliedRate":{"currency":"PHP","unit":1,"rate":22.32,"sourceLabel":"ECB 기준환율 기반 참고값","asOf":"2026-11-06","isMock":true},"transferPath":"/transfer?recipientId=11&amount=500000"}],"latencyMs":4210}
```

운영 값 예(2026-11-19 운영 확인, #101 결과 문서 6절): 500,000 KRW → 23,587 PHP, 1 PHP = 20.926 KRW, `isMock` false.

### 4.4 `VISA_ROADMAP_LINK` (채팅 미연결, 참고)

`ai/tests/fixtures/visa_tools/roadmap_tool_response.json`으로 `roadmap_records`를 실행한 결과.

```json
{"type": "VISA_ROADMAP_LINK", "roadmapId": 31, "stepCount": 4, "applicableFrom": "2026-10-25", "ruleStatus": "DRAFT"}
```

### 4.5 `FX_SUMMARY_CARD`

#101 측정(2026-11-19, 로컬 Spring + 실데이터 백필, 프롬프트 v4.4)에서 "Is now a good time to send money home?"에 나온 카드.

```json
{
  "type": "FX_SUMMARY_CARD",
  "currencies": [
    {
      "currency": "USD", "unit": 1, "rate": 1331.1, "sendRate": 1344.41, "receiveRate": 1317.78,
      "change": -7.7, "changeRate": -0.58, "source": "KOREAEXIM",
      "asOf": "2026-11-19", "fetchedAt": "2026-11-19T11:42:04+09:00",
      "periodAverages": [
        {"period": "1M", "avg": 1335.9208, "diff": -4.8208, "diffRate": -0.36, "direction": "LOWER", "businessDays": 24},
        {"period": "3M", "avg": 1352.3016, "diff": -21.2016, "diffRate": -1.57, "direction": "LOWER", "businessDays": 63},
        {"period": "6M", "avg": 1423.132, "diff": -92.032, "diffRate": -6.47, "direction": "LOWER", "businessDays": 125},
        {"period": "1Y", "avg": 1446.5833, "diff": -115.4833, "diffRate": -7.98, "direction": "LOWER", "businessDays": 246}
      ],
      "isMock": false, "stale": false
    },
    {
      "currency": "PHP", "unit": 1, "rate": 20.926, "sendRate": null, "receiveRate": null,
      "change": -0.058, "changeRate": -0.28, "source": "MARKET_REF",
      "asOf": "2026-11-18", "fetchedAt": "2026-11-19T11:40:29+09:00",
      "periodAverages": [
        {"period": "1M", "avg": 21.0344, "diff": -0.1084, "diffRate": -0.52, "direction": "LOWER", "businessDays": 23},
        {"period": "3M", "avg": 21.5128, "diff": -0.5868, "diffRate": -2.73, "direction": "LOWER", "businessDays": 67},
        {"period": "6M", "avg": 22.9213, "diff": -1.9953, "diffRate": -8.71, "direction": "LOWER", "businessDays": 133},
        {"period": "1Y", "avg": 23.8003, "diff": -2.8743, "diffRate": -12.08, "direction": "LOWER", "businessDays": 255}
      ],
      "isMock": false, "stale": false
    }
  ],
  "unavailableCurrencies": []
}
```

---

## 5. Spring이 그대로 중계하는지 (hanstep-backend develop `f6f3623`)

**blocks는 이름·모양을 바꾸지 않고 그대로 중계한다.** 이벤트 전체 모양은 바뀐다.

- `ChatStreamService.java:316-330` `done()`
  - `:318` `data.get("blocks")`를 JsonNode로 받는다. 없거나 null이면 null(`:319`).
  - `:323-325` `blocks_json`에 `toString()`으로 저장한다.
  - `:328` `chat.done` 전송.
  - type 검사·필터·이름 변경은 없다(모르는 type도 지나간다).
- `ChatStreamEvents.java:94-108` `Done` record(`ChatDoneEvent`): `runId`, `assistantMessageId`, `blocks`(JsonNode, nullable), `latencyMs`.
- 대화방 다시 열기(CHT-03): `ChatSessionService.java:80-84`가 `blocks_json`을 JsonNode로 읽어 `messages[].blocks`로 준다(`ChatMessageListResponse.java:31-32`).
- E-7-4 평가·로드맵 결과(`result_json`)도 JsonNode로 그대로 저장·전달한다. 그래서 엔진의 `questionEn`도 Spring 수정 없이 지나간다(`AiE74Client.java:51`, `:76`, `E74AssessmentService.java:266`, `VisaRoadmapService.java:272`).

AI `agent.done`과 Spring `chat.done`의 차이

| 항목 | AI `agent.done` | Spring `chat.done` |
|---|---|---|
| 이벤트 이름 | `agent.done` | `chat.done` |
| `blocks` | 배열 또는 null | **같은 값 그대로** |
| `content` | 있음(답변 전체) | **없음**. `chat.delta` 조각을 이어 붙이거나 CHT-03 `content`를 읽는다 |
| `runId` | 없음 | 있음 |
| `assistantMessageId` | 없음 | 있음 |
| `latencyMs` | AI 실행 시간 | **Spring이 잰 값**(요청 수신 → 완료) |
| `modelId`, `promptVersion`, `usage`, `timings` | 있음 | 없음(DB·로그에만 저장) |

주의할 점
- Swagger(`ChatOpenApiCustomizer.java:19-37`이 `ChatDoneEvent`를 Schemas에 등록): `blocks`는 JsonNode라 **블록 종류별 스키마가 없고** 설명 문자열(`ChatStreamEvents.java:15-19`)만 있다.
  - 그 설명은 `MISSING_INPUTS 는 fields[]: {field, question}`이라 모양은 맞다. 다만 `question`이 영어라는 말은 없다.
- Swagger 예시는 실제 카드 상수를 쓴다(`ChatController.java:80`, `:102` `REMITTANCE_CARD`, `:119` `FX_SUMMARY_CARD`, `:140` `E74_SCORE_CARD`).
  - Spring 테스트에는 아직 `{"type":"BALANCE_CARD"}`가 있다(`ChatStreamApiTest.java:108`, `:144`, `:162`). **AI에는 `BALANCE_CARD`가 없다.** 테스트만 보고 구현하면 틀린다.
- `blocks_json`이 MySQL `JSON` 컬럼이라(`db/init/01_hanstep_schema.sql:701`) CHT-03로 다시 읽을 때 **객체 키 순서가 chat.done과 다를 수 있다**(MySQL이 JSON 객체 키를 정렬해 저장). 값은 같다. 키 순서에 기대지 않는다.
- 숫자는 Jackson JsonNode를 거쳐 그대로 나간다(정수는 정수, `rate` 22.32는 22.32).

---

## 6. 프론트 처리 합의와 현재 코드 (hanstep-frontend develop `4fb82ba`)

### 6.1 합의

| 합의 | 현재 코드 |
|---|---|
| **의도 확인 칩 없음.** 실서버는 받는 분·통화가 애매하면 텍스트로 되묻고 blocks는 null이다 | 맞음. 옛 Mock `remit_confirm`(의도 확인 칩)은 #34(`9da22e5`)에서 지웠다(`src/api/mock/chat.ts:241` 주석) |
| **`rate_info`는 Mock에만.** 실서버 환율 카드는 `FX_SUMMARY_CARD`다 | 옛 `rate_info` 카드는 #34에서 지웠다. 지금 Mock의 `rateInfo` 응답은 `FX_SUMMARY_CARD`와 같은 모양이다(`mock/chat.ts:175-207`) |
| **추천 질문 칩은 앱 고정(첫 화면만).** 서버는 칩을 주지 않는다 | 맞음. 실서버 모드는 `{ blocks }`만 돌려준다(`src/api/chat.ts:165`). 답변 아래 칩(`suggestions`)은 Mock에만 있다(`chat.ts:16-21`). 첫 화면 질문 4개(`mock/chat.ts:33-38` `LANDING_SUGGESTIONS`, `src/app/(tabs)/chat.tsx:272-293`)와 메뉴는 서버 모드에서도 고정으로 나온다 |
| **송금 이동은 `openTransfer(sendAmount.amount)`** | **다름.** 지금은 카드 '송금하기'가 `transferPath`를 `openTransferPath`로 연다(`RemittanceConfirmCard.tsx:68-70`, `chat.tsx:36-41`). 송금 화면은 주소의 `amount`를 먼저 읽는다(`src/app/transfer/index.tsx:28-45`). 그래서 금액은 `sendAmount.amount`와 같다. `recipientId`는 아직 쓰지 않는다(`:39`). 합의대로 바꿀지 프론트 담당 확인 필요 |

### 6.2 블록 처리

| 단계 | 위치 | 내용 |
|---|---|---|
| 아는 type | `src/api/chat.ts:78-83` | `REMITTANCE_CONFIRM`, `FX_SUMMARY_CARD`, `E74_SCORE_CARD`, `MISSING_INPUTS` |
| 파서 | `src/api/chat.ts:87-93` | 배열이 아니면 `[]`. type이 목록에 있는 것만 남긴다. 안쪽 필드는 검사하지 않는다(모르는 필드는 그대로 지나감) |
| chat.done | `src/api/chat.ts:140-143` | `parseBlocks(d.blocks)` |
| 분기 | `src/features/chat/blocks/ChatBlocks.tsx:24-35` | type별 카드. 모르는 type은 `null` |
| 타입 | `src/types/api.ts:710-766` | 4개 카드 타입과 `ChatBlock` union. `ChatMissingInputsBlock`은 `:757-760` `{field, question}` |
| 대화방 다시 열기(CHT-03) | 없음 | 앱이 아직 부르지 않는다 |

| type | 컴포넌트 | 쓰는 필드 |
|---|---|---|
| `E74_SCORE_CARD` | `E74ScoreCard` | `status`, `total`, `required`, `potentialTotal`, `isScenario`(`ruleStatus`·`ruleVersion`은 안 씀). 버튼 `visa_tab` |
| `MISSING_INPUTS` | `MissingInputsCard` | `fields[].question`을 그대로(`MissingInputsCard.tsx:21`), 3개까지 보이고 나머지는 "외 N개". 제목 '판정에 더 필요한 정보'는 한국어 고정 |
| `REMITTANCE_CONFIRM` | `RemittanceConfirmCard` | `sendAmount` 표시, `transferPath`가 `/transfer`로 시작할 때만 '송금하기'. `fees`·`rateSources`(#105)는 아직 안 쓴다. `receiveAmount`·`appliedRate`가 null인 경우(PHP 참고 환율 없음)도 처리해야 한다 |
| `FX_SUMMARY_CARD` | `FxSummaryCard` | `currency`, `unit`, `rate`, `sendRate`, `receiveRate`, `change`, `changeRate`, `source`, `asOf`, `isMock`, `stale`. `periodAverages`·`unavailableCurrencies`·`fetchedAt`은 안 씀. 버튼(`fx`, `fx_alert`, `transfer`)은 앱 고정 |
| `VISA_ROADMAP_LINK` | 없음 | 파서에서 버린다 |

### 6.3 언어
- `MISSING_INPUTS.question`은 채팅 요청의 사용자 언어로 온다(#108). ko면 한국어, en·vi·zh면 영어다(#103에서는 영어 고정).
- 로드맵 화면(VS-001)은 지금 `missingInputs`의 `question`을 보여 주지 않는다(`field` 접두사만 보고 고정 문구 한 줄, `src/api/visa.ts:313`). 질문을 보여 줄 때는 3.2절의 임시 규칙(`ko`면 `question`, 그 밖이면 `questionEn`)을 따른다.
- `appliedRate.sourceLabel`은 Spring이 준 한국어다. 화면에 쓰지 않고 `rateSources` 코드로 문구를 만든다(예: `KOREAEXIM` → "Export-Import Bank of Korea", `frankfurter-ecb` → "ECB reference rate").
- `fees.notices` 코드도 앱이 화면 언어로 바꾼다(예: `INTERMEDIARY_FEE_MAY_APPLY` → "Intermediary bank fees may apply"). 나머지는 코드·숫자다.

---

## 7. 확인 범위와 하지 않은 것

- AI: develop 코드 읽기 + 로컬 실행(`e74_records`·`roadmap_records` 직접 호출, INT-05 라우트 fake 모드 호출, #101 측정 원자료)으로 확인했다.
- Spring·프론트: `origin/develop` 코드 읽기만 했다(빌드·실행 안 함). `chat.done` 예시는 Spring 코드로 정리한 모양이다.
- 줄 번호는 위 기준 커밋 기준이다. #103에서 바뀐 `ai/app/visa/tools.py`·`ai/app/visa/e74/`는 #103 브랜치 기준이다.

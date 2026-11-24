# LLM 담당 범위와 코드 담당 범위

- 기준 코드: hanstep-AI develop `16c094d`, hanstep-backend develop `aee6ea1`, hanstep-frontend develop `b0d8f6c` (2026-11-23)
- 경로 표기
  - 앞에 아무것도 없으면 hanstep-AI 저장소 루트 기준입니다(예: `ai/app/chat/graph.py:73`).
  - `BE:`는 hanstep-backend 저장소 루트, `BEJ:`는 hanstep-backend의 `src/main/java/com/hanstep/`, `FE:`는 hanstep-frontend 저장소 루트입니다.
- 강제 방식
  - 코드 강제: 코드가 값을 만들거나, LLM 출력을 런타임에 검사해 막거나 바꿉니다.
  - 프롬프트 지시만: 프롬프트에 지시만 있고 코드 검증도 평가도 없습니다.
  - 평가로 확인: 평가 스크립트가 측정하지만 런타임에는 막지 않습니다.
  - 확인 안 됨: 코드로 판단할 수 없거나, 규칙 자체가 코드·프롬프트·평가 어디에도 없습니다.
- 운영 설정은 저장소에 있는 기본값만 확인했습니다. 서버 `.env`가 덮어쓰는 값은 확인하지 않았습니다.

## 1. 한 줄 요약

LLM(Anthropic `claude-haiku-5-5`)은 도구를 고르고 인자를 쓰고 결과를 문장으로 옮기는 일만 합니다. 금액·점수·환율 판정·상품 선택·금리·카드·출처 레코드는 Python과 Spring 코드가 만듭니다. 다만 LLM이 쓴 본문을 런타임에 검사하는 장치는 금융 정책 문장 가드 하나뿐이라, "본문에 도구 결과에 없는 숫자를 쓰지 않는다"는 프롬프트 지시와 평가 스크립트로만 지켜집니다.

## 2. 전체 흐름

```
앱 (CHT-04: 사용자 메시지)
  |
  v
Spring ChatStreamService
  - 메시지의 식별번호 마스킹 (BEJ:chat/service/ChatStreamService.java:121)
  - USER 메시지 저장, language = users.preferred_language, history = 완료된 최근 3쌍
  - AI로 보내는 값: runId, userId, sessionId, language, history, message (BEJ:chat/client/AgentStreamClient.java:27-28)
  |
  v
FastAPI chat_stream (ai/app/api/routes/chat.py)
  - 요청 검증, 프롬프트 버전별 도구 묶음 준비, userId를 도구 생성자에 고정 (ai/app/api/routes/chat.py:225-235)
  - 메시지 = 시스템 프롬프트(언어 코드만 치환) + history + 이번 메시지 (ai/app/chat/stream.py:87-95)
  |
  v
LangGraph (ai/app/chat/graph.py:155-161)
  START -> agent 노드 [LLM 호출: ChatAnthropic + bind_tools, ai/app/chat/graph.py:73, 89]
             |  답변 텍스트 작성, 도구 선택, 인자 작성, 되물을지 판단
             v
           route [코드: tool_calls가 있고 호출 수 < 3이면 tools, 아니면 END, ai/app/chat/graph.py:148-153]
             |
             v
           tools 노드 [코드: 인자 스키마 검증 -> Spring 내부 API / MongoDB 벡터 검색 / 상품 조건표]
             |  LLM에게 줄 결과(허용 필드만) + 화면 카드(blocks) + 출처 레코드
             v
           agent 노드로 돌아감 (LLM 호출 다시)
  |
  v
stream.py
  - 본문 = LLM 텍스트를 이은 것 (ai/app/chat/stream.py:256)
  - 금융 정책 검색이 성공한 턴에만 문장 가드 적용 (ai/app/chat/stream.py:221-227)
  - 카드(blocks)와 출처(sources)는 본문과 별도 필드 (ai/app/chat/stream.py:229-239, 276)
  |
  v
Spring 저장
  - chat_tool_calls, chat_message_sources 저장
  - ASSISTANT 메시지: 본문은 마스킹해서, blocks_json은 그대로 저장 (BEJ:chat/service/ChatStreamService.java:367)
```

도구 안에서 LLM을 부르는 곳은 없습니다. 채팅 밖에서 LLM을 부르는 곳은 비자 로드맵 문장 생성 하나이고, 운영 기본값은 꺼져 있습니다(`ai/app/core/config.py:106`, `BE:deploy/compose.prod.yml:166`).

## 3. 기능별 표

같은 기능의 두 번째 규칙부터는 "LLM이 하는 일"과 "코드가 하는 일" 칸을 비웠습니다.

| 기능 | LLM이 하는 일 | 코드가 하는 일 | 지켜야 할 규칙 | 강제 방식 | 근거(파일:줄) |
|---|---|---|---|---|---|
| 챗봇 그래프 | agent 노드에서 답변 텍스트 작성, 도구 선택, 인자 작성, 되물을지 판단 | 노드 2개(agent, tools)와 route 규칙, 도구 실행, 호출 한도·시간 제한 | 한 턴 도구 호출 3회 이하 | 코드 강제 (한 응답 안의 병렬 호출은 모두 실행됨) | ai/app/chat/graph.py:76, 85, 120, 148-153; ai/app/core/config.py:126 |
| 챗봇 그래프 | | | 한 턴 60초 제한 | 코드 강제 | ai/app/core/config.py:128; ai/app/chat/stream.py:191-196 |
| 챗봇 그래프 | | | 되묻기(재질의). 별도 노드가 없고 LLM이 프롬프트를 보고 판단 | 평가로 확인 | ai/app/chat/graph.py:155-160; ai/app/chat/prompts/remittance_rules.md:8; ai/app/chat/prompts/visa_rules.md:7; ai/scripts/evaluate_chat.py:19-24 |
| Tool Calling | 도구 선택(tool_choice 기본값), 인자 작성 | 프롬프트 버전별 도구 묶음, pydantic 인자 검증, userId 고정, 모르는 도구 거부 | 인자 형식·범위 검증 | 코드 강제 | ai/app/chat/product_tools.py:140-171; ai/app/chat/remittance_tools.py:318-335; ai/app/chat/fx_tools.py:99-107; ai/app/visa/tools.py:61, 76-80; ai/app/chat/finance_policy_tools.py:59-62; ai/app/chat/tools.py:271-275 |
| Tool Calling | | | 다른 사용자 데이터를 조회할 수 없음 | 코드 강제 | ai/app/api/routes/chat.py:225-235; ai/app/chat/tools.py:3-5 |
| Tool Calling | | | 질문에 맞는 도구 선택 | 평가로 확인 | ai/app/chat/prompts/system_v1_1.md:5; ai/scripts/evaluate_chat.py:1208-1209 |
| Tool Calling | | | 인자에는 사용자가 말한 조건만 | 평가로 확인 | ai/app/chat/prompts/product_rules.md:5; ai/scripts/evaluate_chat.py:1218-1226 |
| E-7-4 점수 | 결과 설명, 빠진 입력 되묻기 | 순수 함수로 점수 계산, 승인된 점수표 선택, 미래 기준일을 가정 계산으로 옮김, 카드 생성 | 점수는 코드가 계산 | 코드 강제 | ai/app/visa/e74/engine.py:3, 224-296 |
| E-7-4 점수 | | | 승인된 점수표만 사용 | 코드 강제 (AI와 Spring 양쪽) | ai/app/visa/e74/rules.py:437-445; BEJ:visa/service/E74AssessmentService.java:174-178 |
| E-7-4 점수 | | | 채팅에서 오늘이 아닌 기준일은 가정 계산(scenario) | 코드 강제 (앱 API 경로는 일반 계산으로 저장) | ai/app/chat/visa_tools.py:198-209; BEJ:visa/service/E74AssessmentService.java:107-114 |
| E-7-4 점수 | | | 본문에서 점수를 바꾸거나 추정하지 않음 | 평가로 확인 (E-7-4 회차는 숫자 검사가 미측정이고 카드 값이 본문에 있는지만 봄) | ai/app/chat/prompts/visa_rules.md:6; ai/scripts/compare_chat_versions.py:11-13 |
| E-7-4 점수 | | | 감점 문항(벌금·체납·위반)은 사용자가 답한 것만 넘김 | 프롬프트 지시만 | ai/app/chat/prompts/visa_rules.md:8 |
| E-7-4 점수 | | | 카드에 "모의 사전진단" 표시 | 코드 강제 (앱 고정 문구) | FE:src/features/chat/blocks/E74ScoreCard.tsx:26, 40 |
| E-7-4 점수 | | | 본문에 사전 점검임을 밝힘 | 프롬프트 지시만 ("pre-check") | ai/app/chat/prompts/visa_rules.md:10 |
| 비자 로드맵 | (`ROADMAP_LLM_TEXT`가 켜졌을 때만) 단계 제목·설명 문장 | 단계 선택, 신청 가능일, 서류, 예상 점수, 기준 점수 null, 실패 시 템플릿 문장 | 단계·날짜·서류·점수는 코드가 만듦 | 코드 강제 | ai/app/visa/roadmap/steps.py:58-88, 114-143; ai/app/visa/roadmap/service.py:159-180, 211-229, 269, 312-313; ai/app/visa/roadmap/schemas.py:99-100 |
| 비자 로드맵 | | | LLM 문장의 숫자는 단계 데이터에 있는 것만 | 코드 강제 (아라비아 숫자만 검사, 걸리면 템플릿) | ai/app/visa/roadmap/text.py:67, 286, 302-304 |
| 비자 로드맵 | | | 금지 표현(합격·상위·가능성·보장 등) | 코드 강제 | ai/app/visa/roadmap/text.py:46, 298-301 |
| 비자 로드맵 | | | 입력에 없는 서류·요건·기관을 더하지 않음 | 프롬프트 지시만 | ai/app/visa/roadmap/text.py:193 |
| 비자 정책 RAG | 검색 결과를 읽고 답변 생성 (수집·청크·임베딩·검색 단계에는 LLM 없음) | 매니페스트 수집, 구조 인식 청킹, BGE-M3 임베딩, MongoDB 벡터 검색, 승인 필터, Spring 승인 재확인, 출처 레코드 첨부 | 승인된 현행 문서만 검색 | 코드 강제 (확인 실패 시 결과를 쓰지 않음) | ai/app/rag/retrieval.py:56, 107-130; ai/app/rag/publication.py:25-30; ai/app/chat/visa_tools.py:250-252; BEJ:policy/domain/PolicyDocument.java:119-127 |
| 비자 정책 RAG | | | 출처 레코드(문서 id·청크·쪽) 첨부 | 코드 강제 (실제 인용 여부와 무관하게 검색 결과 전부) | ai/app/visa/tools.py:596-610; ai/app/chat/stream.py:229-231 |
| 비자 정책 RAG | | | 근거가 없으면 "확인되지 않음" | 프롬프트 지시만 (검색 점수 기준 없음) | ai/app/chat/prompts/visa_rules.md:9; ai/app/rag/retrieval.py:172-185 |
| 비자 정책 RAG | | | 검색 결과에 있는 내용으로만 답함 | 프롬프트 지시만 (문장 가드는 금융 정책 검색에만 붙음) | ai/app/chat/prompts/visa_rules.md:9; ai/app/chat/stream.py:221-227 |
| 비자 정책 RAG | | | 답변에 문서명·조항·기준일 표기 | 확인 안 됨 (비자 규칙에 지시가 없고, 시행일·발행일은 LLM에 전달되지 않음) | ai/app/chat/prompts/visa_rules.md:1-11; ai/app/visa/tools.py:580-593 |
| 금융 정책 검색 (외국환거래규정) | 조문 검색 결과로 답변 생성 | BM25 검색, 위험 문장 검사기(R3) | USD 5,000 무증빙 "수령" 조항을 "송금" 허용으로 옮긴 문장 차단 | 코드 강제 (고정 문장으로 교체) | ai/app/finance_policy/answer_guard.py:1-8; ai/app/chat/stream.py:221-227; ai/app/core/config.py:156 |
| 금융 정책 검색 (외국환거래규정) | | | 조문 출처 표기 | 프롬프트 지시만 | ai/app/chat/prompts/finance_policy_rules.md:7 |
| 금융 분석·여유자금 | 분석 결과를 문장으로 옮김, 출처(입력값·과거 평균)를 말함 | 소득·지출·여유자금 계산(Python INT-01, Spring 입력값 기준), 비율·시나리오(judgmentFacts) 계산 | 금액·비율은 코드가 계산 | 코드 강제 | ai/app/finance/calculator.py:124-143; BEJ:finance/service/MonthlyBalanceService.java:44-63; ai/app/chat/judgment.py:70-99, 102-236; ai/app/chat/tools.py:246-249 |
| 금융 분석·여유자금 | | | 본문은 도구 결과의 숫자만 쓰고 새로 계산하지 않음 | 평가로 확인 (number_check는 측정 전용) | ai/app/chat/prompts/system_v1_1.md:6; ai/app/chat/number_check.py:1-3; ai/scripts/evaluate_chat.py:1179-1185 |
| 금융 분석·여유자금 | | | 입력값 기준이면 "입력하신 금액 기준"이라고 말함 | 평가로 확인 | ai/app/chat/prompts/goal_rules.md:2; ai/scripts/_v45_check.py:33-40 |
| 금융 분석·여유자금 | | | Mock 여부를 LLM과 본문에 드러내지 않음(현재 설계) | 코드 강제 (isMock을 LLM에 주지 않음) | ai/app/chat/tools.py:4-5, 136-185; ai/app/chat/prompts/system_v1_1.md:12 |
| 환율 진단 | 비교 결과를 문장으로 옮김("평균보다 낮아요") | Spring이 기간 평균·차이·방향(LOWER/HIGHER/SAME, 0.10% 기준. 코드 주석상 임시값) 계산, AI가 표시 문자열과 카드 생성 | 평균과 높다·낮다 방향 판정 | 코드 강제 | BEJ:fx/service/FxRateService.java:49-50, 131-146; ai/app/chat/fx_tools.py:162-190, 214-215, 289-304 |
| 환율 진단 | | | 본문의 높다·낮다가 방향 값과 일치 | 평가로 확인 | ai/scripts/_fx_check.py:1-17, 109 |
| 환율 진단 | | | 예측·송금 시점 권유 금지 | 평가로 확인 | ai/app/chat/prompts/fx_rules.md:1, 6; ai/scripts/_fx_check.py:161, 174 |
| 환율 진단 | | | "실시간 환율" 표현 금지 | 평가로 확인 (환율 규칙에는 문구가 없고 송금 견적 안내문에만 있음) | ai/scripts/_fx_check.py:168-172; ai/scripts/evaluate_chat.py:1194-1195; ai/app/chat/remittance_tools.py:573-574 |
| 환율 진단 | | | isMock이면 데모 데이터라고 밝힘 | 프롬프트 지시만 (카드에는 앱이 "데모" 태그를 붙임) | ai/app/chat/prompts/fx_rules.md:10; FE:src/features/chat/blocks/FxSummaryCard.tsx:88 |
| 해외송금 판단 | 송금 요청인지 판단, 받는 분·금액·통화 되묻기, 견적 결과 문장 | 조회 도구 3개(모두 GET), 저장된 받는 분·통화·금액 범위 검증, 견적 일치 확인, 확인 카드(이체 화면 경로) | 송금을 실행하지 않음 | 코드 강제 (실행 도구·실행 API가 없고, 앱 제출도 Mock) | ai/app/chat/remittance_tools.py:43-46, 240, 310; ai/app/integrations/spring_client.py:244-288; FE:src/api/transfer.ts:46-53 |
| 해외송금 판단 | | | 저장된 받는 분과 그 통화로만 견적 | 코드 강제 | ai/app/chat/remittance_tools.py:729-744, 793-807 |
| 해외송금 판단 | | | 세 요소가 애매하면 하나만 되묻기 | 평가로 확인 | ai/app/chat/prompts/remittance_rules.md:5-8; ai/scripts/evaluate_chat.py:19-24 |
| 해외송금 판단 | | | 송금 요청이 아니면 송금을 꺼내지 않음(국적·대화로 추측하는 코드 규칙 없음) | 프롬프트 지시만 | ai/app/chat/prompts/remittance_rules.md:12-13 |
| 금융상품 추천 카드 (v4.5~v4.6) | 조건 인자와 검색어 선택, 짧은 결론·확인 못 한 조건·출처 줄 | 상품 필터·순서, 금리는 상품설명서 인용이 있는 조건표 값만, 근거 없으면 null, 금리는 카드에만, 한 턴 1장 | 상품 선택·순서 | 코드 강제 | ai/app/products/recommender.py:209-254; ai/app/chat/product_tools.py:522-554 |
| 금융상품 추천 카드 (v4.5~v4.6) | | | 카드 금리는 상품설명서 근거가 있는 값만 | 코드 강제 | ai/app/chat/product_tools.py:351-380 |
| 금융상품 추천 카드 (v4.5~v4.6) | | | 본문에 금리·우대율을 쓰지 않음 | 평가로 확인 (LLM에는 금리를 주지 않음) | ai/app/chat/product_tools.py:8-9; ai/app/chat/prompts/product_rules.md:10; ai/scripts/_product_check.py:13 |
| 금융상품 추천 카드 (v4.5~v4.6) | | | 카드 내용을 본문에 다시 나열하지 않음(#119) | 프롬프트 지시만 (채점기는 저장소 밖, 영어만 측정) | ai/app/chat/prompts/display_product_card_ko.md:2-6; ai/specs/chat/answer_display_v1.md:324, 355-356 |
| 금융상품 추천 카드 (v4.5~v4.6) | | | 최고 상품이라 부르지 않고 승인을 약속하지 않음 | 프롬프트 지시만 | ai/app/chat/prompts/__init__.py:290-293 |
| 대안신용·대출 진단 | 없음 (채팅에 신용 도구가 없고 대출 추천은 안 된다고 안내) | 로지스틱 회귀 점수, 면책 문구 상수, Spring 표시 필드 | 점수는 코드가 계산 | 코드 강제 | ai/app/credit/alt_score.py:321-343, 406-460; ai/app/api/routes/credit.py:3-4; ai/app/chat/prompts/__init__.py:80-84 |
| 대안신용·대출 진단 | | | "모의 사전진단" 문구 | 코드 강제 (응답 상수. 앱 안내문에는 "모의"가 없음) | ai/app/credit/alt_score.py:41, 447; FE:src/constants/altCredit.ts:31 |
| 대안신용·대출 진단 | | | 채팅에서 승인·신용점수처럼 말하지 않음 | 프롬프트 지시만 | ai/app/chat/prompts/system_v1_1.md:11 |
| 답변 표시(display) | 본문 전체, 표시 문자열이 없는 원화 금액의 표기, 굵은 첫 줄 등 레이아웃 | 환율 표시 문자열(자릿수·반올림·통화명·기준 시각), 송금 안내문, 카드는 본문과 별도 필드 | 환율 표시 문자열 생성 | 코드 강제 | ai/app/chat/display.py:28, 59-61, 76-93, 96-135; ai/app/chat/fx_tools.py:162-190 |
| 답변 표시(display) | | | 카드 값은 도구 결과에서만 | 코드 강제 | ai/app/chat/stream.py:139-142, 232-239, 276; ai/app/visa/tools.py:507-516; ai/app/chat/remittance_tools.py:266-315; ai/app/chat/product_tools.py:425-438 |
| 답변 표시(display) | | | 표시 문자열을 본문에 그대로 옮김 | 평가로 확인 | ai/app/chat/prompts/__init__.py:194-199; ai/scripts/evaluate_chat.py:1179-1185 |
| 답변 표시(display) | | | 굵은 첫 줄·목록·출처 줄 레이아웃 | 프롬프트 지시만 | ai/app/chat/prompts/display_rules_ko.md:2-5 |
| 답변 언어 | 사용자 언어로 답변 작성 | 언어 값 전달·검증, 언어별 표시 규칙 선택 | 사용자 언어 값 전달 | 코드 강제 | BEJ:chat/service/ChatStreamService.java:113; ai/app/chat/schemas.py:37; ai/app/chat/prompts/__init__.py:317-320 |
| 답변 언어 | | | 답변이 그 언어로 나옴 | 평가로 확인 | ai/app/chat/prompts/__init__.py:184-187; ai/scripts/_lang_check.py:3; ai/scripts/evaluate_chat.py:1197-1199 |
| OCR | 없음 (OCR 코드와 의존성에 LLM 없음) | PaddleOCR 인식, 정규식·규칙 추출, 등록번호 마스킹, Spring 형식 재검사, 신뢰도 기준 | OCR에 LLM을 쓰지 않음 | 코드 강제 | ocr/requirements.txt:1-2; ocr/pipeline/engine.py:11, 29-36 |
| OCR | | | 등록번호는 마스킹된 값만 Spring으로 | 코드 강제 (엔진·Worker·Spring 세 단계) | ocr/pipeline/extract.py:24, 90-109; ocr/worker/fields.py:31-38, 86-94, 114; BEJ:document/service/DocumentInternalService.java:111-118 |
| OCR | | | 신뢰도 0.8 미만은 확인 필요 표시 | 코드 강제 (Spring 응답 시 계산) | BEJ:document/dto/DocumentResponse.java:108, 140-146 |
| OCR | | | 로그에 OCR 값·이미지·원본 키를 남기지 않음 | 코드 강제 | ocr/worker/processor.py:105-111, 155-167 |
| 개인정보 | 마스킹된 메시지·history와 허용된 도구 결과만 받음 | 숫자 식별번호 마스킹, 시스템 프롬프트에 사용자 신상 없음, 도구 결과 허용 필드만 | 식별번호는 저장 전·LLM 전송 전 마스킹 | 코드 강제 (정해진 형식만) | BEJ:chat/service/IdentifierMasker.java:21-41; BEJ:chat/service/ChatStreamService.java:121, 137, 367 |
| 개인정보 | | | 시스템 프롬프트에 이름·국적·등록번호를 넣지 않음 | 코드 강제 | ai/app/chat/prompts/__init__.py:317-320; ai/app/chat/stream.py:87-95; BEJ:chat/client/AgentStreamClient.java:27-28 |
| 개인정보 | | | 도구 결과는 허용 항목만(계좌는 끝 4자리) | 코드 강제 | ai/app/chat/tools.py:136-185; ai/app/chat/remittance_tools.py:119-122, 141-148; BEJ:aicontext/dto/AiFinanceContextResponse.java:11-14 |
| 개인정보 | | | 채팅에서 계좌번호를 묻거나 받지 않음 | 프롬프트 지시만 | ai/app/chat/prompts/remittance_rules.md:11 |
| 개인정보 | | | 내부 id를 답변에 쓰지 않음 | 프롬프트 지시만 | ai/app/chat/prompts/system_v1_1.md:12 |

강제 방식별 규칙 수(위 표 기준): 코드 강제 33, 프롬프트 지시만 14, 평가로 확인 13, 확인 안 됨 1.

## 4. 프로젝트 제약 점검표

| 제약 | 어디서 | 방식 | 판정 |
|---|---|---|---|
| 금액·비율·점수는 코드가 계산하고, LLM은 도구 결과에 없는 숫자를 만들지 않음 | 계산: ai/app/finance/calculator.py:124-143, BEJ:finance/service/MonthlyBalanceService.java:44-63, ai/app/visa/e74/engine.py:224-296, BEJ:fx/service/FxRateService.java:131-146. 본문 숫자: ai/app/chat/prompts/system_v1_1.md:6, ai/app/chat/number_check.py:1-3, ai/scripts/evaluate_chat.py:1179-1185 | 계산은 코드 강제. 본문 숫자는 평가로 확인(런타임 검사 없음). 예외로 로드맵 LLM 문장은 코드가 숫자를 검사하고(ai/app/visa/roadmap/text.py:302-304), 상품 금리는 LLM에 주지 않습니다(ai/app/chat/product_tools.py:8-9) | 계산은 지켜짐, 본문은 평가로만 확인 |
| 대안신용·대출 진단·E-7-4는 모의 사전진단으로만 표현 | 대안신용: ai/app/credit/alt_score.py:41. E-7-4 카드: FE:src/features/chat/blocks/E74ScoreCard.tsx:26, 40. E-7-4 본문: ai/app/chat/prompts/visa_rules.md:10. 대출: ai/app/chat/prompts/__init__.py:80-84 | 대안신용 응답과 E-7-4 카드는 코드 강제. E-7-4 본문은 프롬프트 지시만이고 표현도 "pre-check"입니다. E-7-4 계산 API 응답에는 모의 여부 필드가 없습니다(ai/app/visa/e74/engine.py:104-122). 앱의 신용 안내문에는 "모의"가 없습니다(FE:src/constants/altCredit.ts:31) | 일부만 지켜짐 |
| E-7-4 점수표는 DRAFT, 기준 점수는 "공식 확인 후 표시" | ai/app/visa/e74/rule_sets/E74-2026-v1.json:5, 7-8; ai/app/visa/tools.py:512; FE:src/features/chat/blocks/E74ScoreCard.tsx:32 | 점수표는 2026-11-17에 APPROVED로 바뀌었고 기준 점수 200점이 카드와 앱에 그대로 나옵니다. DRAFT일 때 기준 점수를 숨기는 코드는 없습니다. 기준 점수 값 자체가 없을 때만 확인 필요로 둡니다(ai/app/visa/e74/requirements.py:422-429). 로드맵은 기준 점수를 null로 둡니다(ai/app/visa/roadmap/schemas.py:99-100) | 제약과 구현이 다름 |
| 비자 정책 RAG 답변에 근거 문서(제목·조항·기준일), 근거 없으면 "확인되지 않음" | ai/app/visa/tools.py:580-610; ai/app/chat/prompts/visa_rules.md:9; ai/app/rag/retrieval.py:172-185 | 출처 레코드(문서 id·청크·쪽)는 코드가 붙입니다. 답변 속 문서명·조항·기준일 표기는 지시가 없고, 기준일은 LLM에 전달되지 않습니다. "확인되지 않음"은 프롬프트 지시만 있습니다 | 일부만 지켜짐 |
| 송금은 실행하지 않음(Mock 화면 연결) | ai/app/chat/remittance_tools.py:43-46, 240, 310; ai/app/integrations/spring_client.py:244-288; FE:src/api/transfer.ts:46-53 | 코드 강제. 실행 도구와 실행 API가 없고, 확인 카드는 이체 화면으로만 이어지며 앱 제출은 Mock입니다 | 지켜짐 |
| 근거 없는 우대율, "상위 n%", "실시간 환율" 표현 금지 | 우대율: ai/app/chat/product_tools.py:351-380, ai/app/chat/prompts/product_rules.md:10, ai/scripts/_product_check.py:13. 실시간: ai/app/chat/remittance_tools.py:573-574, ai/scripts/_fx_check.py:168-172. 상위 n%: 채팅 경로에서 찾지 못함(로드맵 금지어에만 있음, ai/app/visa/roadmap/text.py:46) | 카드 금리는 코드 강제. 본문 우대율과 "실시간"은 평가로 확인. "상위 n%"는 채팅 경로에 규칙이 없습니다(백분위를 계산하는 값도 없음) | 일부만 지켜짐 |
| 마이데이터 Mock은 isMock 표시 | ai/app/chat/tools.py:4-5; ai/app/chat/prompts/system_v1_1.md:12; FE:src/api/mydata.ts:2; BE:db/init/01_hanstep_schema.sql:466 | LLM에 isMock을 주지 않고, 프롬프트는 Mock 언급을 금지하며, 앱 마이데이터 화면도 표시하지 않습니다. 환율·송금 카드만 isMock이면 "데모" 태그를 붙입니다(FE:src/features/chat/blocks/FxSummaryCard.tsx:88, FE:src/features/chat/blocks/RemittanceConfirmCard.tsx:41) | 지켜지지 않음 |
| 챗봇 응답은 영어 | ai/specs/chat/answer_language_v1_design.md:8, 11-12; ai/app/chat/prompts/__init__.py:184-187; ai/scripts/_lang_check.py:3 | 설계가 "사용자 언어로 답하기"로 바뀌었습니다. 언어 값 전달은 코드 강제, 실제 답변 언어는 평가로만 확인합니다. 운영에서 en 사용자에게 한국어로 답한 기록이 있습니다 | 제약과 구현이 다름 |

## 5. 빈틈과 위험

고치는 방법은 한 줄 제안만 적었습니다. 이 문서 작업에서는 고치지 않았습니다.

### 프롬프트 지시만이거나 평가로만 확인하는 규칙

| 우선순위 | 빈틈 | 이유 | 제안 |
|---|---|---|---|
| 높음 | 본문 숫자가 도구 결과에 있는지 런타임 검사가 없음 (ai/app/chat/number_check.py:1-3, ai/app/chat/stream.py:208-219) | 금액·점수 오기가 그대로 사용자에게 가고 저장됩니다. E-7-4 회차는 평가에서도 숫자 검사를 하지 않습니다(ai/scripts/compare_chat_versions.py:11-13) | 로드맵의 숫자 검사(ai/app/visa/roadmap/text.py:302-304)와 같은 방식으로 done 단계에서 검사해 로그나 경고를 남깁니다 |
| 높음 | 비자 정책 "확인되지 않음"이 프롬프트에만 있고 검색 점수 기준이 없음 (ai/app/chat/prompts/visa_rules.md:9, ai/app/rag/retrieval.py:172-185) | 관련 없는 청크도 늘 상위 결과로 들어가 체류·비자 오답으로 이어질 수 있습니다 | 검색 점수 기준 아래면 도구가 "결과 없음"을 돌려주고 고정 문구로 답하게 합니다 |
| 높음 | 비자 답변이 검색 결과에 근거하는지 검사가 없고, 출처는 인용 여부와 무관하게 검색 결과 전부를 저장 (ai/app/chat/stream.py:221-231) | 화면의 출처가 실제 근거가 아닐 수 있습니다. 비자 정책 답변의 평가 결과도 저장소에 없습니다 | 비자 정책 질문 세트로 근거 일치 평가부터 만듭니다 |
| 중간 | E-7-4 감점 문항(벌금·체납·위반)을 LLM이 추측해 넣을 수 있음 (ai/app/chat/prompts/visa_rules.md:8) | 잘못 넣으면 점수와 판정이 바뀝니다 | 감점 인자는 이번 대화에서 사용자가 답한 경우만 받도록 도구가 확인 질문 결과를 요구합니다 |
| 중간 | 송금 금액·통화가 실제 사용자 말에서 왔는지 코드가 확인하지 못함 (ai/app/chat/prompts/remittance_rules.md:5-8) | 확인 카드와 이체 화면이 뒤에서 막지만, 잘못된 견적 카드가 나올 수 있습니다 | 견적 인자의 금액이 사용자 메시지에 있는지 코드로 확인합니다(judgment.py가 메시지 금액을 읽는 방식 재사용) |
| 중간 | 환율 예측·송금 시점 권유·"실시간" 표현이 평가로만 확인되고, 환율 규칙에는 "실시간" 금지 문구가 없음 (ai/app/chat/prompts/fx_rules.md:1, 6) | 금융 조언 오해를 낳을 수 있는 표현입니다 | 평가의 금지 표현 목록(ai/scripts/_fx_check.py:168-172)을 런타임 문장 가드에 연결합니다 |
| 중간 | 본문 금리·우대율 금지가 평가로만 확인됨 (ai/app/chat/prompts/product_rules.md:10) | LLM은 금리 데이터를 받지 않으므로, 말한다면 지어낸 값입니다 | 상품 턴의 본문에서 % 숫자를 찾으면 로그를 남깁니다 |
| 중간 | 답변 언어가 평가로만 확인됨 (ai/scripts/_lang_check.py:3) | 운영에서 언어가 틀린 기록이 있습니다(ai/specs/chat/answer_language_v1_design.md:8) | 평가용 언어 검사를 done 단계 로그에 붙입니다 |
| 중간 | 금융 정책 런타임 가드가 한 가지 문장 유형만 봄 (ai/app/finance_policy/answer_guard.py:1-8) | 나머지 조문 해석은 프롬프트에만 맡깁니다. 운영 compose의 `FINANCE_POLICY_CORPUS_DIR` 기본값이 비어 있어(BE:deploy/compose.prod.yml:158) 서버 설정에 따라 검색과 가드가 꺼질 수 있습니다 | 운영 `.env` 값을 확인하고 가드 규칙을 평가 결과에 따라 늘립니다 |
| 낮음 | 로드맵 LLM 문장이 입력에 없는 서류·요건·기관을 더할 수 있음 (ai/app/visa/roadmap/text.py:193) | 운영 기본값에서는 LLM 문장 생성이 꺼져 있습니다(BE:deploy/compose.prod.yml:166) | 켜기 전에 서류 이름 목록 검사를 추가합니다 |
| 낮음 | 카드 내용 재나열·레이아웃 규칙이 프롬프트에만 있음 (ai/app/chat/prompts/display_product_card_ko.md:2-6) | 보기 문제이고, 한국어는 측정하지 않았습니다 | 채점기를 저장소에 넣고 한국어도 측정합니다 |
| 낮음 | 채팅에서 계좌번호를 묻지 않음, 내부 id를 쓰지 않음이 프롬프트에만 있음 (ai/app/chat/prompts/remittance_rules.md:11, ai/app/chat/prompts/system_v1_1.md:12) | 계좌번호는 Spring이 저장 전에 마스킹합니다 | 본문의 내부 id 패턴을 로그로 셉니다 |
| 낮음 | 상품 규칙 두 줄이 v4.6에 함께 들어가 서로 부딪힘 (ai/app/chat/prompts/product_rules.md:10, ai/app/chat/prompts/goal_rules.md:12) | "앱에서 금리를 확인하라"와 "카드에서 금리를 본다"가 같이 들어갑니다 | v4.6 조립에서 한 줄을 뺍니다 |

### 코드 쪽 빈틈

| 우선순위 | 빈틈 | 이유 | 제안 |
|---|---|---|---|
| 중간 | 식별번호 마스킹의 구분자가 하이픈과 공백 한 칸뿐 (BEJ:chat/service/IdentifierMasker.java:21) | en dash·em dash·점으로 쓴 등록번호는 가려지지 않고 저장되며 LLM에도 갑니다. OCR 쪽 정규식은 `[-–—~]`를 처리합니다(ocr/pipeline/extract.py:24). 정규식을 읽고 판단했고 실행해 보지는 않았습니다 | 구분자를 OCR 쪽과 같게 넓히고 평가 세트에 사례를 넣습니다 |
| 낮음 | 한 응답 안의 병렬 도구 호출은 모두 실행돼 한도 3회를 넘을 수 있음 (ai/app/chat/graph.py:120, 151) | 호출 수 제한이 응답 단위로만 걸립니다 | 실행 전에 남은 횟수만큼만 실행합니다 |
| 낮음 | 숫자가 아닌 개인정보(이름·주소·이메일·영문이 섞인 여권번호)는 가리지 않음 (BEJ:chat/service/IdentifierMasker.java:7-17) | 대상이 숫자 식별번호로 한정돼 있습니다 | 범위를 문서에 명시하고 여권번호 패턴을 검토합니다 |

### 제약과 구현이 다른 곳

| 우선순위 | 내용 | 이유 | 제안 |
|---|---|---|---|
| 높음 | 마이데이터 Mock이 채팅과 앱 화면 어디에도 표시되지 않음 (ai/app/chat/tools.py:4-5, ai/app/chat/prompts/system_v1_1.md:12, FE:src/api/mydata.ts:2) | DB 주석(BE:db/init/01_hanstep_schema.sql:466)과 제약이 표시를 요구합니다 | 마이데이터 화면과 분석 결과에 Mock 표시를 넣을지 팀이 정합니다 |
| 중간 | E-7-4 점수표가 APPROVED이고 기준 점수 200점이 화면에 나옴 (ai/app/visa/e74/rule_sets/E74-2026-v1.json:5, 7-8) | 규칙 파일의 note에는 2026-11-17 팀이 원문과 대조해 승인했다고 적혀 있습니다. 제약 문구가 최신 결정을 반영하지 않았을 수 있습니다 | 발표 자료와 지침을 현재 결정에 맞춥니다 |
| 중간 | 챗봇 응답이 영어 고정이 아니라 사용자 언어 (ai/specs/chat/answer_language_v1_design.md:11-12) | 설계 문서가 영어 고정을 목표로 하지 않는다고 적었습니다 | 발표 자료와 지침을 현재 설계에 맞춥니다 |

## 6. 면접 답변용 요약

- 챗봇은 LangGraph의 agent·tools 두 노드로 돌고, LLM은 agent 노드에서 도구를 고르고 인자를 쓰고 결과를 문장으로 옮기는 일만 합니다(ai/app/chat/graph.py:73, 148-161).
- 금액·여유자금·환율 평균과 높다·낮다 판정·E-7-4 점수·상품 선택과 금리·송금 견적은 Python과 Spring 코드가 계산합니다. 화면 카드는 도구 결과로 코드가 만들어 본문과 따로 보냅니다(ai/app/chat/stream.py:232-239, 276).
- 도구 인자는 pydantic 스키마로 검증하고, 사용자 id는 요청마다 코드가 고정해 LLM이 다른 사람 데이터를 부를 수 없습니다. 송금은 실행 API가 없어 확인 카드와 이체 화면으로만 이어집니다(ai/app/api/routes/chat.py:225-235, ai/app/chat/remittance_tools.py:43-46).
- 개인 식별번호는 Spring이 저장과 LLM 전송 전에 가리고, OCR은 LLM 없이 PaddleOCR과 규칙으로 읽어 등록번호를 마스킹한 값만 넘깁니다(BEJ:chat/service/ChatStreamService.java:121, ocr/pipeline/extract.py:90-109).
- LLM이 쓴 본문 숫자가 도구 결과에 있는지는 런타임이 아니라 평가 스크립트로 측정하고 있고, 이 부분을 런타임 검사로 옮기는 것이 다음 개선 지점입니다(ai/app/chat/number_check.py:1-3).

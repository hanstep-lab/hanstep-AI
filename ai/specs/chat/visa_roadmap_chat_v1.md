# 챗봇 비자 로드맵 도구·카드 v1 (#127, 프롬프트 v4.7)

- 작성일: 2026-11-23
- 배경: 챗봇에서 "내 비자 승급하려면 어떻게 해?" 같은 질문에 로드맵 카드가 나오지 않았다(#123 조사). `get_visa_roadmap`이 챗봇 도구로 연결돼 있지 않았기 때문이다. 입력 형식·카드(`VISA_ROADMAP_LINK`)·`roadmap_records`와 Spring 챗봇 도구용 API(`POST /internal/v1/ai-context/users/{userId}/visa-roadmaps`)는 이미 있었다.
- 기준 커밋: hanstep-AI `develop` `33431aa`(Merge #126), hanstep-backend `develop` `aee6ea1`(Merge #169)
- 블록 형식은 `chat_blocks_v1.md` 3.4절.

## 1. 바꾼 것

| 항목 | 내용 | 위치 |
|---|---|---|
| 프롬프트 v4.7 | v4.6 그대로 + 바꾸는 두 줄 + 끝에 로드맵 규칙(`roadmap_rules.md`). v4.6 이하 문구는 그대로 | `ai/app/chat/prompts/__init__.py` `ROADMAP_CHANGES`·`_with_roadmap_rules` |
| 도구 연결 | `get_visa_roadmap`은 v4.7에서만 붙는다(`ROADMAP_PROMPT_VERSIONS`). v4.7에는 v4.6의 도구가 모두 붙는다 | `ai/app/api/routes/chat.py` |
| 도구 실행 | Spring 로드맵 API 한 번(재시도 없음, 시간 제한 `CHAT_TOOL_TIMEOUT_SECONDS`). 기준일은 늘 오늘(KST). 오늘이 아닌 `asOfDate`는 REJECTED `INVALID_ARGUMENTS`(저장된 로드맵이 앱 비자 탭의 최신 로드맵이 되기 때문) | `ai/app/chat/visa_tools.py` `VisaTools._create_roadmap`, `ai/app/integrations/spring_client.py` `visa_roadmap` |
| 실패 구분 | E-7-4와 같은 규칙(`_spring_result`). 로드맵 이유 `NO_STAY_EXPIRY`·`STAY_EXPIRED`를 더했다 | `ai/app/chat/visa_tools.py` `ROADMAP_FAILURES` |
| LLM 도구 설명 | 영어(`ROADMAP_TOOL_DESCRIPTION`). `asOfDate`는 "Leave it out" | `ai/app/visa/tools.py` |
| 재질의 언어 | 로드맵 `MISSING_INPUTS`·LLM 결과의 질문도 사용자 언어(ko 한국어, 그 밖 영어). 전에는 카드 영어·LLM 한국어로 갈렸다 | `ai/app/visa/tools.py` `roadmap_records(response, language)` |

### 1.1 v4.7에서 바꾼 두 줄

- 점수 규칙(`visa_rules.md`에서 온 줄): "점수·신청 가능 여부·점수 올리는 법 → `calculate_e74_score`"에서 "점수 올리는 법"을 빼고 로드맵 규칙을 따르게 했다.
- 목표 규칙 SETTLEMENT 줄(`goal_rules.md`에서 온 줄): 열린 조언 질문에서는 점수 도구를 부르지 않는 것은 그대로이고, 질문 자체가 한국 장기 체류·비자 변경이면 로드맵 규칙을 따르게 했다(4절, 2026-11-24 수정).

### 1.2 로드맵 규칙(`roadmap_rules.md`) 요약

- 사용자 자신의 체류와 E-7-4 길은 `get_visa_roadmap`을 먼저 부른다: 한국 장기 체류·정착, 비자 전환·승급, 무엇을 어떤 순서로 준비할지, 점수 올리는 법, 로드맵, 비자·체류 만료가 언제인지·얼마나 남았는지. E-7-4라는 말이 없고 일반 조언처럼 들려도 같다. 같은 질문에 점수 도구·정책 검색·금융 분석을 같이 부르지 않는다.
- 로드맵은 E-7-4만이다. 다른 비자 종류(F-2·F-5 등) 질문은 비자 규칙(정책 검색)을 따른다.
- 단계·점수·날짜는 도구 결과 그대로. 모든 단계를 나열하지 않는다: 첫 줄(지금 신청 가능한지, 아니면 이유나 신청 가능일. 만료 질문이면 `stay.stayExpiresOn`·`stay.daysToExpiry`를 먼저), 다음 단계 최대 3개(제목 + 추가 점수나 목표일), 마지막 줄(기준일, 전체는 카드에, 사전 점검이며 비자 결정이 아님).
- 누락 입력은 두세 개씩 묻는다. 실패하면 단계를 지어내지 않고 '비자 로드맵' 메뉴를 안내한다.

## 2. 전제·범위 밖

- 단계 문장(`title`·`description`) 언어는 Spring이 정한다. hanstep-backend #170·#171(develop `30bbb8d`)부터 로드맵·E-7-4 계산 요청 `language`(#125·#126)에 `users.preferred_language`를 넣는다(ko면 넣지 않음). 그 전 Spring은 늘 한국어로 만들었다.
- 앱 카드(hanstep-frontend): `VISA_ROADMAP_LINK`를 모르는 type이라 버린다. **운영 기본값을 v4.7로 올리기 전에 FE 카드가 먼저 나가야 한다.**
- 운영 기본값(hanstep-backend `deploy/compose.prod.yml` `CHAT_PROMPT_VERSION`)은 v4.6 그대로다.
- 채팅 로드맵 요청에 감점·제외 6문항(`legalDeclaration`)은 넣지 않는다. Spring은 받을 준비가 돼 있다(`VisaRoadmapToolRequest`). 그래서 `MISSING_INPUTS`가 보통 붙는다.
- `ROADMAP_LLM_TEXT=true`면 Spring 로드맵 생성이 LLM 문장 때문에 도구 시간 제한(5초)을 넘을 수 있다. 운영 기본값은 false이고 측정도 false로 했다.

## 3. 측정 (2026-11-23, 로컬)

- 스택: MySQL(hanstep-backend `develop` `db/init` 새로 적재) · Spring `hanstep-backend:disp-6e76668`(develop `aee6ea1`과 채팅·비자·`db/init` 같음) · AI `hanstep-ai:local` + 이 브랜치 코드, `CHAT_PROMPT_VERSION=v4.7`, `CHAT_LLM_MODE=anthropic`(claude-haiku-5-5), `E74_INCLUDE_DRAFT_RULES=false`, `ROADMAP_LLM_TEXT=false`
- 방법: Reyes(`demo-e9-ph-reyes`)로 Spring CHT-01·CHT-04를 앱처럼 불렀다. 질문마다 새 대화방, 3회. 사용자 언어는 MySQL `users.preferred_language`(en·ko)로 바꿨다. 도구 상태·오류 코드·지연은 `chat_tool_calls`에서 읽었다.

| 질문(en / ko) | 도구 (en·ko 각 3회) | blocks |
|---|---|---|
| How can I upgrade my visa? / 내 비자 승급하려면 어떻게 해? | `get_visa_roadmap` 6/6 | `VISA_ROADMAP_LINK`, `MISSING_INPUTS` |
| What should I prepare first to switch to E-7-4? / E-7-4로 바꾸려면 뭐부터 준비해야 해? | `get_visa_roadmap` 6/6 | 같음 |
| Show my visa roadmap / 내 비자 로드맵 보여줘 | `get_visa_roadmap` 6/6 | 같음 |
| How can I raise my E-7-4 score? / E-7-4 점수 올리려면 어떻게 해? | `get_visa_roadmap` 6/6 | 같음 |
| Can I switch to E-7-4 visa? / E-7-4 비자로 바꿀 수 있어? | `get_visa_roadmap` 6/6 | 같음 |
| What is my E-7-4 score? / 내 E-7-4 점수 알려줘 | `calculate_e74_score` 6/6 | `E74_SCORE_CARD`, `MISSING_INPUTS` |
| What documents do I need for E-7-4? / E-7-4 신청할 때 필요한 서류가 뭐야? | `search_visa_policies` ×2, 6/6 | null |

- 도구 상태는 모두 SUCCEEDED, 오류 코드 없음. 로드맵 도구 지연 19~193ms(처음 생성 193ms, 그 뒤는 Spring이 같은 로드맵 행을 다시 줌).
- 답변: 첫 줄 "지금은 신청할 수 없고 2026-12-05부터 가능", 다음 단계 3개(고용기업 추천서 +50점, TOPIK 4급 +40점, 전환 신청 기한 2027-10-04), 재질의, 기준일·카드·사전 점검 줄. 답변의 숫자(235·285·325점, 날짜)는 도구 결과에 있는 값이다(재질의의 KRW 1,000,000은 질문 문구의 값).
- 결정(2026-11-23): "E-7-4로 바꿀 수 있어?"(전환 가능 여부)는 v4.6에서는 점수 카드였지만 v4.7에서는 로드맵 카드가 나온다. 첫 줄에서 가능 여부·가능 날짜를 답하고 할 일까지 보여 주므로 이대로 둔다. 점수 카드는 점수를 직접 물을 때 나온다.
- en 답변 12건 중 3건에 한국어 단계 이름("고용기업 추천서 제출" 등)이나 내부 표기 "(붙임5)"가 섞였다. 이 Spring 이미지는 로드맵 요청에 `language`를 넣지 않아 단계 문장이 한국어로 저장됐기 때문이다(3.1절에서 다시 쟀다).
- 측정하지 않은 것: 정착(SETTLEMENT) 목표가 있는 사용자의 열린 조언 질문(측정 DB의 Reyes는 목표가 없음), 로드맵 실패 경로(프로필·만료일 없음 등, 단위 테스트로만 확인), 운영 환경.

### 3.1 BE `language` 전달 뒤 en 다시 측정 (2026-11-23)

- 바꾼 것: Spring만 hanstep-backend `develop` `30bbb8d`(Merge #171, 로드맵·E-7-4 계산 요청에 `language`)로 새로 빌드했다. MySQL·AI·질문·횟수는 3절과 같다(en 7문항 × 3회). DB에는 3절에서 한국어로 만든 평가·로드맵(id 1)이 남아 있었다.
- 도구·blocks: 3절 en과 같다(로드맵 질문 5종 15/15 `[VISA_ROADMAP_LINK, MISSING_INPUTS]`, 점수 3/3 `[E74_SCORE_CARD, MISSING_INPUTS]`, 서류 3/3 null). 모두 SUCCEEDED.
- 저장: en 요청은 `requestKey`가 달라 새 평가(id 2)와 새 로드맵(id 2)이 만들어졌다. 단계 이름이 영어로 저장됐다("Submit the employer recommendation letter", "Get TOPIK level 4", "Apply to change to E-7-4"). 한국어 로드맵(id 1)은 다시 쓰이지 않았다.
- **en 답변 한글 섞임 0/15**(3절 3/12). 로드맵 도구 지연 25~618ms(새로 만들 때 618ms).
- 단계는 모든 답변에서 3개다(번호 목록이 6줄인 답변은 재질의 질문 3개에도 번호를 붙인 것).

## 4. 첫 화면 칩 보완과 머지 전 통합 테스트 (2026-11-24)

### 4.1 왜 고쳤나
- 운영 Reyes 기록(서버 조회, #123 댓글): 카드 없이 글만 나온 비자 답변은 첫 화면 추천 질문 1번 "한국에서 오래 살고 싶은데 어떻게 해요?"(4회)와 "내가 E-7-4로 전환하려면 어떻게 해야돼?"였다. v4.6에서는 둘 다 `search_visa_policies`라 카드가 없다.
- 3절 v4.7로 같은 질문을 재니 칩 1번은 ko 1/3·en 1/3만 로드맵이었다(나머지는 정책 검색이나 금융 분석만). 질문에 E-7-4가 없어 로드맵 질문으로 읽히지 않았고, SETTLEMENT 목표 줄("부르지 말라")도 열린 조언에서 로드맵을 막았다. 칩 4번 "비자 만료까지 얼마나 남았어요?"도 점수 카드·로드맵·정책 검색으로 갈렸다.
- 그래서 로드맵 규칙 첫 줄에 장기 체류·정착·비자 변경·만료일을 넣고(E-7-4가 없어도), 다른 비자 종류는 정책 검색으로 남기고, 만료 질문이면 첫 줄에 만료일·남은 일수를 쓰게 했다. SETTLEMENT 줄은 질문 자체가 장기 체류·비자 변경이면 로드맵 규칙을 따르게 했다.

### 4.2 통합 테스트
- 스택: MySQL(`db/init` + `db/dev` 환율 Mock, `FX_INCLUDE_MOCK_RATES=true`) · Spring hanstep-backend `develop` `30bbb8d` · AI 이 브랜치(수정 후 v4.7, 비교 기준 v4.6) · Mongo. Reyes로 Spring CHT-01·CHT-04를 앱처럼 불렀다.
- 질문: 첫 화면 칩 4개(ko·en 화면 문구 그대로), 비자 질문 5개, 환율·상품. 조건: 목표 없음(22문항 × 3회), 정착 목표(SETTLEMENT·LIVING_COST, 8문항 × 3회). 버전마다 90건.

| 질문(ko / en) | v4.6 | v4.7 |
|---|---|---|
| 칩1 한국에서 오래 살고 싶은데 어떻게 해요? / I want to live in Korea long-term. What should I do? (목표 없음·정착 목표) | 카드 없음 12/12 | `VISA_ROADMAP_LINK` 12/12 |
| 칩2 본국에 50만 원 보내면 얼마 받아요? / If I send ₩500,000 home… | 카드 없음 6/6(받는 분 되묻기) | 같음 6/6 |
| 칩3 이번 달 얼마까지 저축할 수 있어요? / How much can I save this month? | 카드 없음 12/12(금융 분석) | 같음 12/12 |
| 칩4 비자 만료까지 얼마나 남았어요? / How long until my visa expires? | 목표 없음: ko `E74_SCORE_CARD` 3·en 카드 없음 3 / 정착 목표: 카드 없음 6 | `VISA_ROADMAP_LINK` 12/12(첫 줄 만료일·남은 일수) |
| 내 E-7-4 점수 / What is my E-7-4 score? | `E74_SCORE_CARD` 6/6 | 같음 6/6 |
| 전환 방법 / How can I switch to E-7-4? | 카드 없음 6/6 | `VISA_ROADMAP_LINK` 6/6 |
| 로드맵 보여줘 / Show my visa roadmap | 없음 5·점수 카드 1 | `VISA_ROADMAP_LINK` 6/6 |
| 서류 / documents, F-2 비자 조건, visa information plz | 카드 없음 12/12 | 같음 12/12(정책 검색) |
| 환율 / PHP exchange rate | `FX_SUMMARY_CARD` 6/6 | 같음 6/6 |
| 적금 추천 / savings product | `PRODUCT_RECOMMEND_CARD` 6/6 | 같음 6/6 |
| 정착 목표 + 열린 조언(요즘 돈 관리 뭐부터 / What should I focus on…) | 금융 분석 6/6 | 같음 6/6(비자 도구 안 부름) |

- v4.7: 기대와 다른 답 0/90, 도구 실패 0, en 답변 한글 섞임 0. v4.6: 정책 검색 FAILED 2건(로컬 검색), 한글 섞임 0.
- 칩2(송금)는 두 버전 모두 저장된 받는 분이 둘이라 먼저 되묻는다. 받는 분을 고르면 다음 턴에 `REMITTANCE_CONFIRM`이 나오는 원래 동작이다(이 PR과 무관).
- 하지 않은 것: MyData Mock(거래 내역)은 넣지 않아 금융 분석 답변 내용은 보지 않았다(도구·카드만).
- 주의: 측정 DB는 `db/init` 시드라 `policy_documents`가 모두 DRAFT였다. 정책 검색이 Spring 승인 확인에서 모두 걸러져(결과 0건) 정책 질문(서류·F-2·비자 정보) 답은 내용이 모두 "확인되지 않음"이었다. 위 표의 도구·카드 판정은 유효하지만 정책 답변 내용은 이 측정으로 보지 않았다. 4.3절에서 승인 상태를 Mongo·운영과 같게(1~6 APPROVED, 7 RETIRED) 맞춘 뒤 서류 질문을 다시 확인했다. 운영 `policy_documents`도 같은 상태다(2026-11-24 서버 조회).

### 4.3 앱 통합 테스트 (실기기, 2026-11-24)

- 앱: hanstep-frontend `feat/70-visa-roadmap-card`(PR #71)를 Expo Go로 실행, `EXPO_PUBLIC_API_URL`을 4.2절 스택(Spring을 PC LAN 포트로 연결)으로 바꿨다. Reyes는 측정 DB에서 온보딩 6단계 완료·MyData 연결 상태로 맞췄다.
- 영어(en)·한국어(ko) 화면 모두 확인했다(사람 확인). 서버 기록(`chat_messages`·`chat_tool_calls`)도 같았다.
  - 칩1(장기 체류)·칩4(비자 만료): `VISA_ROADMAP_LINK` + `MISSING_INPUTS` 카드가 화면 언어로 보이고, 카드 버튼으로 비자 탭 로드맵이 열린다
  - 점수 `E74_SCORE_CARD`, 환율 `FX_SUMMARY_CARD`, 상품 `PRODUCT_RECOMMEND_CARD`, 송금 `REMITTANCE_CONFIRM`(원화 금액) 그대로
  - 서류 질문: 정책 문서 승인 상태를 맞춘 뒤 검색 5건, 고용노동부 추천·농축산 분야 서류를 출처와 함께 답하고 법무부 최종 서류는 확인되지 않음 + 하이코리아 안내
- 시연 대본 송금 시나리오는 재질의 답을 원화로 바꿨다("원화로, 필리핀에 계신 엄마한테"). 페소 금액 요청은 Spring 견적 API가 KRW만 받아(BE #122, `remittance_contract_v1.md` 3절) REJECTED `SPRING_HTTP_400`으로 끝난다. v4.6에서도 같고 이 PR과 무관하다.

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
- 목표 규칙 SETTLEMENT 줄(`goal_rules.md`에서 온 줄): 열린 조언 질문에서는 `get_visa_roadmap`도 부르지 않는다(v4.6과 같은 동작).

### 1.2 로드맵 규칙(`roadmap_rules.md`) 요약

- 사용자 자신의 E-7-4 길(전환·승급 방법, 무엇을 어떤 순서로 준비할지, 점수 올리는 법, 로드맵)은 `get_visa_roadmap`을 먼저 부른다. 같은 질문에 점수 도구·정책 검색을 같이 부르지 않는다.
- 단계·점수·날짜는 도구 결과 그대로. 모든 단계를 나열하지 않는다: 첫 줄(지금 신청 가능한지, 아니면 이유나 신청 가능일), 다음 단계 최대 3개(제목 + 추가 점수나 목표일), 마지막 줄(기준일, 전체는 카드에, 사전 점검이며 비자 결정이 아님).
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

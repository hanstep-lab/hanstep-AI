# 필리핀 국적 사용자 현지화(프롬프트 v4.8) 측정 기록 v1 (#130)

## 1. 무엇을 쟀나

- v4.8 = v4.7 + 현지화 블록(`app/chat/prompts/ph_localization_en.md`). 블록은 요청 `nationalityCode == "PHL"`이고 `language == "en"`일 때만 붙는다. 그 밖의 경우 v4.8 글은 v4.7과 같다(`tests/unit/test_chat_prompts.py`).
- 확인한 것: (1) 기존 세트(eval·remit·lang)가 v4.8 + 국적 PHL에서 나빠지지 않는가, (2) 현지화 규칙을 지키는가, (3) 토큰·지연이 얼마나 느는가.
- 아래 숫자는 모두 이 측정에서 나온 값이다. 측정은 한 번(현지화 세트 3회 반복, 회귀 세트 1회)이라 작은 차이는 반복 간 흔들림일 수 있다.

## 2. 측정 조건

| 항목 | 값 |
|---|---|
| 날짜 | 2026-11-24. 기준선 UTC 02:40~02:50(구현 전 코드), v4.8·현지화 세트 UTC 03:22~03:37(이 PR 코드) |
| 대상 | `evaluate_chat.py --target ai --llm-mode anthropic`, 로컬 격리 스택(AI 컨테이너 + Spring + MySQL, 망 분리). 운영 아님 |
| 모델 | claude-haiku-5-5, thinking model_default, effort 없음, maxTokens 2000, judgmentFacts true |
| 프롬프트 | `X-Chat-Config` promptVersion으로 v4.7·v4.8 지정(`CHAT_ALLOW_CONFIG_OVERRIDE=true`, `CHAT_PH_LOCALIZATION=true`) |
| 사용자 | 데모 사용자 user 1(가상 인물 Reyes, 국적 PHL), MyData 연결, 정책 문서 1~6 APPROVED |
| 요청 | `--language en --nationality PHL`(현지화 세트의 대조군 문항은 문항 값 우선: PH10 ko, PH11 국적 없음) |
| 기준선 | v4.7, 같은 세트를 구현 전에 측정(`ph_baseline_v47_*`, 같은 날, 같은 스택. 기준선은 국적을 보내지 않았고 v4.7은 국적을 쓰지 않는다) |
| 반복 | 현지화 세트 11문항 × 3회 × 2버전(같은 실행에서 번갈아), 회귀 세트 1회 |
| 결과 파일 | `ai/artifacts/chat/ph_baseline_v47_{eval,remit,lang}`, `ph_v48_{eval,remit,lang}`, `ph_v47_v48_ph`, `ph_v48_tokens.md`(커밋하지 않는 폴더) |
| 비용 | 기준선 $0.3270, v4.8 회귀 $0.3396, 현지화 세트 $0.1532 |

## 3. 시스템 프롬프트 토큰 (count_tokens, 도구 정의 제외)

`scripts/count_chat_tokens.py --question-set system --versions v4.7,v4.8 --languages en,ko --nationalities none,PHL --turns first,later`

| 프롬프트 | 언어 | 국적 | 턴 | 토큰 | v4.7 대비 |
|---|---|---|---|---|---|
| v4.7 | en | 없음·PHL | 모두 | 6045 | – |
| v4.7 | ko | 없음·PHL | 모두 | 6221 | – |
| v4.8 | en | 없음 | 모두 | 6045 | 0 |
| v4.8 | en | PHL | 첫 턴 | 6569 | +524 |
| v4.8 | en | PHL | 그 밖의 턴 | 6564 | +519 |
| v4.8 | ko | 없음·PHL | 모두 | 6221 | 0 |

## 4. 회귀: v4.7 기준선 vs v4.8 + 국적 PHL

| 세트 | 버전 | 답변 | 도구 일치 | 숫자 검사 통과 | 언어 판정(inLanguage) | 한글 섞임 | 송금 판정 correct | 첫 글자 중앙값(ms) | 입력 토큰 평균 | 비용(USD) |
|---|---|---|---|---|---|---|---|---|---|---|
| eval | v4.7 | 49 | 49/49 | 47/49 | 48/49 | 1 | – | 2958 | 23884 | 0.1320 |
| eval | v4.8 | 49 | 49/49 | 48/49 | 48/49 | 1 | – | 2932 | 24930 | 0.1384 |
| remit | v4.7 | 30 | 25/30 | 29/30 | 26/30 | 4 | 24/30 | 2655 | 28391 | 0.1618 |
| remit | v4.8 | 30 | 26/30 | 29/30 | 24/30 | 6 | 24/30 | 2758 | 28858 | 0.1669 |
| lang | v4.7 | 10 | 7/10 | 4/10 | 9/10 | 1 | – | 4269.5 | 28618 | 0.0332 |
| lang | v4.8 | 10 | 7/10 | 4/10 | 9/10 | 1 | – | 4339.5 | 29846 | 0.0344 |

- 회귀 세트 89답변(+송금 2턴째 15답변)에서 v4.8은 허용 표현을 한 번도 쓰지 않았다. 아래 현지화 지표 위반도 0이다.
- remit 한글 섞임 4 → 6: 늘어난 것은 모두 앱 메뉴 이름 `'해외송금' (overseas remittance)`이다(S04·S21·S24, 견적이 안 될 때 메뉴를 안내하는 답변). 메뉴 이름을 한국어 + 영어 설명으로 쓰는 것은 v4.7 `goal_rules.md` 규칙이고, v4.7 기준선에도 같은 문구가 3번 있다(S07·S14·S19). 필리핀어 표현으로 생긴 판정은 없다.

## 5. 현지화 세트 (`specs/chat/ph_localization_questions_v1.json`, 채점 `scripts/_ph_check.py`)

### 5.1 버전별 합계 (각 33답변)

| 지표 | 목표 | v4.7 | v4.8 |
|---|---|---|---|
| 허용 표현을 쓴 답변 | – | 0 | 9 |
| 답변당 최대 표현 수 | ≤ 2 | 0 | 2 |
| 2개 초과 답변 | 0 | 0 | 0 |
| 첫 턴이 아닌데 인사("Kumusta po") | 0 | 0 | 0 |
| 허용 목록 밖 Tagalog 단어 | 0 | 0 | 0 |
| 숫자가 있는 문장 속 표현 | 0 | 0 | 0 |
| 불만·오류 답변(PH05) 속 표현 | 0 | 0/3 | 0/3 |
| "English only" 뒤 답변(PH06) 속 표현 | 0 | 0/3 | 0/3 |
| 대조군(PH10 ko·PH11 국적 없음) 속 표현 | 0 | 0/6 | 0/6 |
| 금지 문구(사랑·희생·의무·종교·명절 등, `FORBIDDEN`) | 0 | 0 | 0 |
| 가족 단정(`your wife` 등, 사람 확인용) | 0 | 0 | 0 |
| 도구 일치(문항의 기대 도구) | – | 29/33 | 33/33 |
| 첫 글자 중앙값(ms) | – | 3250 | 3540 |
| 입력 토큰 평균 | – | 19986 | 19176 |
| 비용(USD) | – | 0.0771 | 0.0762 |

### 5.2 문항별 표현 사용 (v4.8, 3회)

| 문항 | 역할 | 3회 답변의 표현 |
|---|---|---|
| PH01 | 첫 턴 인사 | Kumusta po! / Kumusta po! / Kumusta po! |
| PH02 | 3턴째 대화 | 없음 / 없음 / 없음 |
| PH03 | 송금 금액 | 없음 / 없음 / 없음 |
| PH04 | 환율 | 없음 / 없음 / 없음 |
| PH05 | 송금 실패·불만 | 없음 / 없음 / 없음 |
| PH06 | English only 뒤 | 없음 / 없음 / 없음 |
| PH07 | 대화 종료 | Salamat po!, Ingat po! / Ingat po! / Ingat po! |
| PH08 | 가족 송금 부담 | 없음 / 없음 / 없음 |
| PH09 | 설정 완료·감사 | Maraming salamat po! / Maraming salamat po! / Maraming salamat po! |
| PH10 | ko 대조군 | 없음 / 없음 / 없음 |
| PH11 | 국적 없음 대조군 | 없음 / 없음 / 없음 |

### 5.3 답변 예 (원문 앞부분, 1회째)

- PH01 v4.8: "**Kumusta po! I can help you with your money and your visa plans in Korea.**" (v4.7: "**I can help with your money and your visa questions in this app.**")
- PH07 v4.8: "Thanks for chatting with us today. Salamat po! Ingat po!"
- PH09 v4.8: "**Maraming salamat po! Your profile setup is complete.**"
- PH05 v4.8(불만): "**I'm sorry about the stress. I can't see the status of that transfer here, so please check it with Shinhan directly.**" — 표현 없음
- PH08 v4.8(가족 부담): "**You have KRW 675,317 of spare money each month after your usual remittance, so you can support your family and still save for yourself.**" — 표현·금지 문구 없음

### 5.4 코드 채점 위반 사례

- 없음. 현지화 세트 66답변, 회귀 세트 v4.8 104답변(2턴째 포함)에서 `_ph_check` 지표 위반 0건.

## 6. 기존 검사와의 충돌 확인

- 언어 판정(`scripts/_lang_check.py`): en 답변은 한글 혼입만 본다. 허용 표현 4개는 로마자라 걸리지 않는다(단위 테스트 `test_allowed_expressions_pass_the_existing_answer_checks`). 측정에서도 표현을 쓴 9답변 중 8답변이 inLanguage이고, 나머지 1답변(PH01)은 앱 메뉴 이름 4개(한국어 + 영어 설명) 때문이다. 그래서 예외 목록은 더하지 않았다.
- 숫자 검사(`app/chat/number_check.py`): 표현에 숫자가 없어 영향이 없다(같은 테스트). 표현을 쓴 9답변 모두 숫자 검사 통과.
- 답변 검사기(`app/finance_policy/answer_guard.py` v3): 5,000 + 송금 동사 문장만 본다. 표현 문장은 걸리지 않는다(같은 테스트).
- 표시 규칙(`display_rules_en.md`): 블록은 금액·환율·조건을 표준 영어로 쓰라는 내용이라 충돌하지 않는다. 측정에서 숫자가 있는 문장 속 표현 0건.

## 7. 한계

- 회귀 세트는 1회라 remit 언어 판정 26 → 24 같은 차이는 반복 간 흔들림과 구분하지 못한다(차이의 원인은 4절 메뉴 이름).
- 현지화 세트 문항·기대 도구·Tagalog 단어 목록·금지 문구는 이 이슈에서 정했다. 금지 문구·가족 단정은 단어 일치라 표현을 바꾼 문장은 놓칠 수 있다.
- PH06(E-7-4 점수)의 숫자 검사는 두 버전 모두 3/3 불통과다. evaluate_chat 숫자 근거에 `calculate_e74_score` 결과가 들어가지 않아서다(측정 도구 한계, 두 버전 같음).
- 지연은 로컬 Docker(Windows)에서 잰 값이다. 현지화 세트 첫 글자 중앙값 +290ms, 회귀 세트 −26 ~ +103ms로 방향이 섞여 있다.
- 사람이 읽고 판단할 항목(어조, 가족 부담 답변의 적절성)은 5.3 예 외에는 따로 채점하지 않았다.

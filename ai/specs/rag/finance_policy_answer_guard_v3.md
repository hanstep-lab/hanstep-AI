# 금융 정책 답변 위험 문장 검사 규칙 v3 (#93, 실행 전 고정, 마지막 규칙 수정)

v2(`finance_policy_answer_guard_v2.md`) 결과(`reviews/finance_policy_answer_guard_v2_result.md`)는 오탐 1(after A10#3)로 불통과였다.
원인: D2가 서류 단어와 부정 표현의 거리·관계를 보지 않아, 같은 문장 다른 절의 'not'을 부정으로 잡았다.

## 0. 공개·중단 규칙

- **v3는 v2 오탐(after A10#3 "Amounts above USD 5,000 in one transfer would need documents, so split transfers may not be the way around that.")을 본 뒤 만든 규칙이다.**
  v1 누락 2문장(after A15v#1, N04#3)도 이미 본 상태다. 기존 파일에 대한 v3 결과는 **표본 내 성적**이다.
- **규칙 수정은 v3가 마지막이다.** 불통과면 보고하고 멈춘다. v4는 제안하지 않는다.

## 1. 문장 나누기·신호 M·S·R

v1 1·2절과 같다(정규식 그대로).

## 2. D3 (무증빙, v3)

아래 셋 중 하나가 문장에 있으면 D3다. 영어는 대소문자 무시. '단어'는 공백으로 나뉜 `[\w'’-]+` 토큰이며, 쉼표 등 문장 부호가 끼면 이어지지 않는다.

| 묶음 | 뜻 | 정규식(Python `re`, IGNORECASE) |
|---|---|---|
| (a) | 부정 표현 뒤 0~3단어 안에 서류 단어 | `\b(?:without\|no\|do\s+not\s+need\|does\s+not\s+need\|don['’]t\s+need\|doesn['’]t\s+need\|needn['’]t\|no\s+need\s+for)(?:\s+[\w'’-]+){0,3}?\s+(?:documents?\|paperwork\|proof\|evidence)\b` |
| (b) | 서류 단어 뒤 0~3단어 안에 부정 서술 | `\b(?:documents?\|paperwork\|proof\|evidence)(?:\s+[\w'’-]+){0,3}?\s+(?:not\s+required\|not\s+needed\|not\s+necessary\|are\s+not\|is\s+not)\b` |
| (c) | 한국어 | `(?:증빙\|서류)[^\s.]{0,3}\s?(?:없이\|없어도\|필요\s?없)` |

- (a)의 'no'는 단어 'no'만이다('not'·'now' 아님).
- (c)는 '증빙 없이', '서류 없이', '증빙서류 없이', '증빙이 필요 없다', '서류는 없어도'를 잡는다.

## 3. 규칙

| 규칙 | 조건(한 문장 안) |
|---|---|
| R3 | M ∧ S ∧ ¬R ∧ D3 |

- 한 답변에 걸린 문장이 하나라도 있으면 그 답변이 걸린 것으로 센다. 혼합(M ∧ S ∧ R)은 참고로만 센다.

## 4. 통과 기준 (상한 포함, 하나라도 어기면 불통과)

1. 확정 위험 탐지 **8/8**(after A01#1·A01v#2·A01v#3·A02#1·A03#3·A15v#1·N04#3, merged A03#1)
2. 오탐 **0**(상한 0)
3. 혼합 문장이 걸리지 않음

## 5. 실행

- 대상·제외: v1·v2와 같은 6개 파일.
- `ai/scripts/eval/check_policy_answer_guard.py --rule v3`. v1·v2 출력은 바꾸지 않는다(다시 만들어 기존 결과 md와 비교).
- 외부 호출 없음, 서버 코드 변경 없음.

## 6. 적용 설계 (구현 전 고정, 2026-11-18)

v3 0원 실행(`reviews/finance_policy_answer_guard_v3_result.md`)이 통과해 서버 답변 경로에 넣는다. AI 서버만 바꾸고 Spring·앱은 바꾸지 않는다.

### 6-1. 위치·동작

- 위치: `ai/app/chat/stream.py` `run_chat`, **문장 단위 버퍼**. 한 턴에서 `search_finance_policy`가 SUCCEEDED로 끝난 뒤의
  답변 호출 delta만 버퍼에 넣는다. 그 밖의 턴과 도구 전 텍스트는 지금처럼 바로 내보낸다(v4.1은 이 도구가 없어 영향 없음).
- 설정 `FINANCE_POLICY_ANSWER_GUARD` = `off` | `log` | `replace`, **기본값 `replace`**(운영 env에 키가 빠져도 안전한 쪽).
  - `off`: 검사하지 않는다(지금과 같음).
  - `log`: 버퍼 없이 바로 내보내고, 같은 텍스트를 따로 모아 문장마다 검사해 적중만 기록한다(지연 없음, 답변 그대로).
  - `replace`: 문장이 완성될 때마다 검사해 내보낸다. 걸린 문장은 아래 대체 문장으로 바꿔 **delta와 `agent.done` content에 똑같이** 내보낸다.
- 대체 문장(영어 한 문장):
  "Under Article 4-4(4) of the Foreign Exchange Transaction Regulation, the no-document allowance of up to USD 5,000 per transaction applies only to money you receive, not to money you send abroad."
- 한 답변에서 여러 문장이 걸려도 대체 문장은 **한 번만** 넣고, 나머지 걸린 문장은 뺀다.
- 규칙은 v3(M ∧ S ∧ ¬R ∧ D3). 신호·D3 정규식은 서버 모듈 하나에 두고 평가 스크립트가 그것을 쓴다(규칙 중복 없음).

### 6-2. 문장 나누기(스트리밍)

- 경계: 마침표·물음표·느낌표 뒤 공백, 그리고 줄바꿈(평가 스크립트 1절처럼 줄도 나눈다).
- 끊지 않는 곳: 약어 `U.S.`·`e.g.`·`i.e.`·`No.`·`Art.`·`etc.` 뒤, 소수점·금액(`5,000.00`처럼 마침표 뒤가 공백이 아님).
- 마지막 글자가 마침표면 다음 글자를 볼 때까지 기다린다. 정상 종료 때 남은 버퍼는 검사 뒤 내보낸다.
- **오류·끊김·시간 초과 때 검사하지 않은 마지막 조각은 내보내지 않는다**(공개). Spring은 받은 delta까지만 저장한다.
- 평가 스크립트의 문장 나누기(1절)는 바꾸지 않는다(v1·v2·v3 결과 재현). 서버 쪽은 약어 처리만 더 있다.

### 6-3. 기록

- 적중 로그: 기존 `chat run=… outcome=…` 요약 로그 옆 한 줄. runId, promptVersion, rule=v3, hits, D3 갈래(a/b/c), action(log/replace)만.
  **질문·답변 원문, 금액, 사용자 id는 남기지 않는다.**
- `agent.metrics`(`X-Chat-Metrics: 1`일 때만): mode, 버퍼 사용 여부, hits, 갈래, **대체 전 원문 문장**(측정 전용, 로그·DB에 저장하지 않음).
  Spring은 `agent.metrics`를 저장하지 않는다(모르는 이벤트 무시).
- `agent.done` `timings.firstDeltaMs`는 **실제로 내보낸 첫 delta** 시각이다(버퍼를 쓰면 그만큼 늦어진다).

### 6-4. 표본 밖 측정 기준 (측정 전 고정)

- 측정: 46문항 × 3, ② 설정(프롬프트 v4.2 + `FINANCE_POLICY_LABELS=true`) + `FINANCE_POLICY_ANSWER_GUARD=replace`, 16-1절 조건(로컬 8001), 같은 develop 코드 + 이 브랜치.
- 통과 기준(순서대로, 하나라도 어기면 불통과):
  1. 화면·저장 답변의 위험 오답 노출 **0**(대체 뒤 답변 기준, 사람 판정)
  2. 적용 대상 오류 **0**
  3. 정답 **≥ 60**(answerable 96회, 11-1절·17·19절 채점 규칙 그대로)
  4. 금융 정책 턴(search_finance_policy를 부른 회차)의 첫 delta 중앙값 증가 **≤ 500ms**. 기준선 = merged 결과
     (`policy_merged_labels_v4_2.jsonl`)의 같은 턴, evaluate_chat `timings.firstDeltaMs`(클라이언트 측) 중앙값 4,769ms → 5,269ms 이하.
- 오탐: 대체가 일어난 회차를 모두 기록하고 대체 전 원문(metrics)으로 사람이 확인한다. 오탐 대체 때문에 판정이 내려간 회차는 따로 표시한다.
- 누락 확인: v1 R1(M ∧ S ∧ ¬R)을 넓은 그물로 대체 뒤 답변에 돌려, 걸린 문장을 사람이 검토한다.
- 이 측정도 표본 내 문항(46문항)이다. 문장 수준에서는 새 답변이라 v3 규칙의 표본 밖 확인이 된다.

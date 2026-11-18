# 금융 정책 답변 위험 문장 검사 규칙 v1 (#93, 실행 전 고정)

목적: 제4-4조 ④(비거주자·외국인거주자는 건당 미화 5천불 이내면 증빙 없이 **수령**)를 **송금** 허용으로 옮긴 답변 문장을 기계로 찾는다.
규칙은 조문 의미(④ = 무증빙 건당 5천불은 '수령')에서 정했다. 확정 위험 답변 문장을 보고 맞추지 않는다.
이 문서를 커밋한 뒤 기준선 스크립트를 처음 실행한다. 실행 결과를 본 뒤 규칙을 바꾸면 v2로 따로 적는다.

## 1. 문장 나누기

- 답변 본문을 줄바꿈과 문장 끝(`.`·`!`·`?` 뒤 공백)으로 나눈다. 목록 기호(`-`, `*`, `1.`)는 문장 앞에서 뗀다.
- 대소문자는 구분하지 않는다.

## 2. 신호

| 신호 | 뜻 | 정규식(Python `re`, IGNORECASE) |
|---|---|---|
| M | 금액 USD 5,000 | `(?<![\d,.])(?:US\$\|USD\s?\|\$)?\s?5,000(?![\d]\|,\d)`, `(?<![\d,.])(?:US\$\|USD\s?\|\$)?\s?5000(?!\d)`, `\b5k\b`, `\bfive\s+thousand\b`, `5천\s?(?:달러\|불)` |
| S | 송금 방향 | `\b(?:send\|sends\|sending\|sent)\b`, `\btransfer\w*`, `\bremit\w*`, `\bwire\w*`, `보내`, `송금` |
| R | 수령 방향 | `\breceiv\w*`, `\bincoming\b`, `\binbound\b`, `받`, `수령` |
| D | 무증빙 | `\bwithout\s+(?:supporting\s+)?(?:documents?\|proof\|evidence)\b`, `\bno\s+documents?\b`, `증빙\s?없이` |

- M의 '5,000 dollars'·'USD 5,000'·'US$5,000'·'$5,000'은 위 첫 식에 들어간다. '15,000'·'5,000,000'은 M이 아니다.

## 3. 규칙

| 규칙 | 조건(한 문장 안) |
|---|---|
| R1 | M ∧ S ∧ ¬R |
| R2 | R1 ∧ D |
| 혼합(따로 셈) | M ∧ S ∧ R |

- 한 답변에 걸린 문장이 하나라도 있으면 그 답변이 걸린 것으로 센다.

## 4. 고르는 기준 (순서대로)

1. 확정 위험 오답 탐지 **8/8**
2. 오탐 최소
3. 단순함(동점이면 R1)

- **확정 위험 8건**: 설계 문서 17-3절 after v4.2(`policy_after_v4_2.jsonl`)의 A01#1, A01v#2, A01v#3, A02#1, A03#3, A15v#1, N04#3과
  머지 코드 재측정(`policy_merged_labels_v4_2.jsonl`)의 A03#1(`reviews/finance_policy_merged_v4_2_review.md`, 태건 확정).
- **오탐**: 사람 확정 판정이 위험 오답이 아닌 답변에서 걸린 것. 걸린 문장 내용이 실제로 틀렸으면 '오탐 아님 후보'로 표시만 하고 분류는 사람이 한다.

## 5. 기준선 대상 (0원, 저장된 결과만)

| 파일 | 내용 | 포함 |
|---|---|---|
| `policy_baseline_v4_1.jsonl` | 46문항 × 3, v4.1 기준선 | 포함 |
| `policy_after_v4_2.jsonl` | 46문항 × 3, v4.2 after | 포함 |
| `policy_guard_v4_3a.jsonl` | 46문항 × 3, ① v4.3a | 포함 |
| `policy_guard_labels_v4_2.jsonl` | 46문항 × 3, ② v4.2 + 라벨 | 포함 |
| `policy_merged_labels_v4_2.jsonl` | 46문항 × 3, develop 머지 코드 재측정 | 포함 |
| `regression_v4_2.jsonl` | 회귀 49문항 × 3, v4.2 | 포함 |
| `policy_baseline_v4_1_toolsdown.jsonl` | 24문항 × 3, 도구 장애(무효, 10절) | 제외(목록만) |
| `policy_baseline_v4_1_stopped-llm-mode.jsonl`, `policy_smoke_*.jsonl` | 1회 호출(중단·스모크) | 제외(목록만) |
| hold-out 결과 | 레포 밖 | 제외(열지 않음) |

- 스크립트: `ai/scripts/eval/check_policy_answer_guard.py`(입력 jsonl 여러 개 → md, 외부 호출 없음).
- 결과: 파일별 답변 수 / R1 걸림 / R2 걸림 / 혼합 / 확정 위험 탐지(/8) / 오탐 / 걸린 문장 원문 전부.
- 이 단계는 서버 답변 경로 코드를 바꾸지 않는다. 적용은 결과 확인 뒤 따로 정한다.

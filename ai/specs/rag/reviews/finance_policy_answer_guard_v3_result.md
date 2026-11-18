# 금융 정책 답변 위험 문장 검사 0원 실행 (#93, 규칙 v3)

규칙: `ai/specs/rag/finance_policy_answer_guard_v3.md`(실행 전 커밋).
저장된 답변만 읽었다(외부 호출 없음, 서버 코드 변경 없음).
R3 = M ∧ S ∧ ¬R ∧ D3(부정 표현·서류 단어 0~3단어 근접, 한국어 증빙·서류 없이). 혼합 = M ∧ S ∧ R(참고, 검출 아님).
**v3는 v2 오탐(after A10#3)과 v1 누락 문장을 본 뒤 만든 규칙이라 아래는 표본 내 성적이다. 규칙 수정은 v3가 마지막이다.**

## 1. 파일별 결과

| 파일 | 답변 수 | R3 걸림 | 혼합(답변) | 확정 위험 탐지 (/8) | 오탐 |
|---|---|---|---|---|---|
| policy_baseline_v4_1.jsonl | 138 | 0 | 0 | 0 | 0 |
| policy_after_v4_2.jsonl | 138 | 7 | 4 | 7 | 0 |
| policy_guard_v4_3a.jsonl | 138 | 0 | 2 | 0 | 0 |
| policy_guard_labels_v4_2.jsonl | 138 | 0 | 3 | 0 | 0 |
| policy_merged_labels_v4_2.jsonl | 138 | 1 | 10 | 1 | 0 |
| regression_v4_2.jsonl | 147 | 0 | 0 | 0 | 0 |
| **합계** | 837 | 8 | 19 | **8** (/8) | **0** |

- 확정 위험 중 놓친 것: 없음
- 제외(목록만): policy_baseline_v4_1_toolsdown.jsonl(도구 장애, 무효), policy_baseline_v4_1_stopped-llm-mode.jsonl(1회, 중단), policy_smoke_*.jsonl(1회, 스모크), hold-out 결과(레포 밖, 열지 않음)

## 2. 통과 기준 (규칙 문서 4절, 하나라도 어기면 불통과)

| 기준 | 값 | 판정 |
|---|---|---|
| 1. 확정 위험 탐지 8/8 | 8/8 | 통과 |
| 2. 오탐 0(상한 0) | 0 | 통과 |
| 3. 혼합 문장 미검출 | 혼합이 걸린 문장 0 | 통과 |
| **종합** | | **통과** |

## 3. 걸린 문장 전부 (R3)

| 파일 | 문항#회차 | 확정 위험 | 사람 판정 | 메모 | 문장 |
|---|---|---|---|---|---|
| policy_after_v4_2.jsonl | A01#1 | 예 | risky_wrong |  | For you, the rule that applies is Article 4-4 (effective 2026-11-02), which lets a foreign resident send up to USD 5,000 per transaction without supporting documents. |
| policy_after_v4_2.jsonl | A01v#2 | 예 | risky_wrong |  | Under Foreign Exchange Transaction Regulation, Article 4-4, paragraph 4 (effective 2026-11-02), non-residents and foreign residents can send up to 5,000 dollars per transaction without supporting documents. |
| policy_after_v4_2.jsonl | A01v#3 | 예 | risky_wrong |  | For foreign residents, Article 4-4(4) (effective 2026-11-02) allows sending without supporting documents only for a single payment of up to 5,000 dollars. |
| policy_after_v4_2.jsonl | A02#1 | 예 | risky_wrong |  | Under Article 4-4(4), a single transaction of up to US$5,000 can be sent without supporting documents. |
| policy_after_v4_2.jsonl | A03#3 | 예 | risky_wrong |  | Under Foreign Exchange Transaction Regulation, Article 4-4 (effective 2026-11-02), both foreign residents and non-residents can send up to US$5,000 per transaction without supporting documents, and wages earned in Korea can be sent through a designated foreign exchange bank. |
| policy_after_v4_2.jsonl | A15v#1 | 예 | risky_wrong |  | Under Article 4-4(4) (effective 2026-11-02), transfers of up to USD 5,000 per transaction do not need supporting documents. |
| policy_after_v4_2.jsonl | N04#3 | 예 | risky_wrong |  | Under the Foreign Exchange Transaction Regulation, Article 4-4(4) (effective 2026-11-02), a foreigner resident in Korea can send up to USD 5,000 per transaction without submitting supporting documents. |
| policy_merged_labels_v4_2.jsonl | A03#1 | 예 | wrong |  | Instead, Article 4-4 (effective 2026-11-02) applies, which lets you send income from domestic work through a designated foreign exchange bank, and allows a single transfer of up to USD 5,000 without supporting documents. |

## 4. 참고: 혼합 문장 중 D3가 있는 것 (검출 아님)

| 파일 | 문항#회차 | 문장 |
|---|---|---|
| policy_after_v4_2.jsonl | A10#2 | Under the regulation, Foreign Exchange Transaction Regulation, Article 4-4(4) (effective 2026-11-02), a foreign resident can receive money without submitting proof documents when each transfer is up to USD 5,000. |
| policy_after_v4_2.jsonl | A10v#2 | Under the regulation, Foreign Exchange Transaction Regulation Article 4-4(4) (effective 2026-11-02) lets a foreign resident receive up to USD 5,000 per transaction without submitting proof of the transfer. |
| policy_guard_v4_3a.jsonl | A10v#3 | Article 4-4(4) says a foreigner resident can receive money without submitting supporting documents when each transfer is within US$5,000 (Foreign Exchange Transaction Regulation, Article 4-4, paragraph 4, effective 2026-11-02). |
| policy_guard_labels_v4_2.jsonl | A10#1 | Under the regulation, a foreign worker living in Korea can receive a transfer of up to US$5,000 per transaction without submitting supporting documents (Foreign Exchange Transaction Regulation, Article 4-4 ④, effective 2026-11-02). |
| policy_guard_labels_v4_2.jsonl | A10#2 | Under the regulation, you do not need to submit supporting documents to receive a transfer of up to USD 5,000 per transaction. |
| policy_guard_labels_v4_2.jsonl | A10v#2 | Under the regulation, Foreign Exchange Transaction Regulation, Article 4-4 ④ (effective 2026-11-02), a non-resident or foreign resident can receive a transfer without submitting proof documents when it is within 5,000 US dollars per transaction. |
| policy_merged_labels_v4_2.jsonl | A10#3 | Under the regulation, Foreign Exchange Transaction Regulation, Article 4-4 ④ (effective 2026-11-02), a foreign resident can receive a transfer without submitting proof of the transaction when a single transfer is USD 5,000 or less. |
| policy_merged_labels_v4_2.jsonl | A10v#1 | Under the regulation, a foreign resident can receive up to US$5,000 per transaction without submitting supporting documents, so a US$3,000 transfer from your brother falls within that limit (Foreign Exchange Transaction Regulation, Article 4-4 ④, effective 2026-11-02). |
| policy_merged_labels_v4_2.jsonl | A10v#2 | Under the regulation, Article 4-4 ④ (effective 2026-11-02), a foreign resident can receive money without submitting proof documents if each transfer is within 5,000 US dollars. |
| policy_merged_labels_v4_2.jsonl | A10v#3 | Under the regulation, Article 4-4 ④ (effective 2026-11-02), a non-resident or foreign resident can receive money without submitting supporting documents when each transfer is up to US$5,000. |

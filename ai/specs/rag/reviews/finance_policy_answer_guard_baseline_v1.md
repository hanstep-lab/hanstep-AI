# 금융 정책 답변 위험 문장 검사 0원 기준선 (#93, 규칙 v1)

규칙: `ai/specs/rag/finance_policy_answer_guard_v1.md`(실행 전 커밋).
저장된 답변만 읽었다(외부 호출 없음, 서버 코드 변경 없음).
R1 = M ∧ S ∧ ¬R, R2 = R1 ∧ D, 혼합 = M ∧ S ∧ R. 오탐 = 확정 위험 8건 밖에서 걸린 답변.

## 1. 파일별 결과

| 파일 | 답변 수 | R1 걸림 | R2 걸림 | 혼합 | 확정 위험 탐지 R1 / R2 (/8) | 오탐 R1 / R2 |
|---|---|---|---|---|---|---|
| policy_baseline_v4_1.jsonl | 138 | 0 | 0 | 0 | 0 / 0 | 0 / 0 |
| policy_after_v4_2.jsonl | 138 | 20 | 5 | 4 | 7 / 5 | 13 / 0 |
| policy_guard_v4_3a.jsonl | 138 | 15 | 0 | 2 | 0 / 0 | 15 / 0 |
| policy_guard_labels_v4_2.jsonl | 138 | 16 | 0 | 3 | 0 / 0 | 16 / 0 |
| policy_merged_labels_v4_2.jsonl | 138 | 12 | 1 | 10 | 1 / 1 | 11 / 0 |
| regression_v4_2.jsonl | 147 | 0 | 0 | 0 | 0 / 0 | 0 / 0 |
| **합계** | 837 | 63 | 6 | 19 | **8 / 6** (/8) | **55 / 0** |

- 확정 위험 중 R1이 놓친 것: 없음
- 확정 위험 중 R2가 놓친 것: policy_after_v4_2.jsonl A15v#1, policy_after_v4_2.jsonl N04#3
- 제외(목록만): policy_baseline_v4_1_toolsdown.jsonl(도구 장애, 무효), policy_baseline_v4_1_stopped-llm-mode.jsonl(1회, 중단), policy_smoke_*.jsonl(1회, 스모크), hold-out 결과(레포 밖, 열지 않음)

## 2. 고르는 기준 적용 (규칙 문서 4절 순서)

| 규칙 | 1. 확정 위험 탐지(/8) | 2. 오탐(답변) | 3. 단순함 | 결과 |
|---|---|---|---|---|
| R1 | 8 | 55 | 신호 3개 | 선택 |
| R2 | 6 | 0 | 신호 4개 | 기준 1 미달 |

- R1 오탐 55답변 중 걸린 문장이 모두 소액해외송금업 한도(제2-31조) 문장인 답변 52, 그 밖 3(분류 보조, 판정 아님).

## 3. 걸린 문장 전부

'사람 판정'은 판정 파일 값이다. 머지 재측정은 리뷰 문서 answerable 표를 읽었고,
회귀·머지 not_in_docs처럼 판정이 없는 곳은 '-'다.
'오탐 아님 후보'는 확정 위험 밖에서 걸렸지만 문장 내용이 틀렸을 수 있는 것이다
(Claude Code 표시, 분류는 사람).

| 파일 | 문항#회차 | 규칙 | 확정 위험 | 사람 판정 | 오탐 아님 후보 | 문장 |
|---|---|---|---|---|---|---|
| policy_after_v4_2.jsonl | A01#1 | R2 | 예 | risky_wrong |  | For you, the rule that applies is Article 4-4 (effective 2026-11-02), which lets a foreign resident send up to USD 5,000 per transaction without supporting documents. |
| policy_after_v4_2.jsonl | A01v#2 | R2 | 예 | risky_wrong |  | Under Foreign Exchange Transaction Regulation, Article 4-4, paragraph 4 (effective 2026-11-02), non-residents and foreign residents can send up to 5,000 dollars per transaction without supporting documents. |
| policy_after_v4_2.jsonl | A01v#3 | R2 | 예 | risky_wrong |  | For foreign residents, Article 4-4(4) (effective 2026-11-02) allows sending without supporting documents only for a single payment of up to 5,000 dollars. |
| policy_after_v4_2.jsonl | A02#1 | R2 | 예 | risky_wrong |  | Under Article 4-4(4), a single transaction of up to US$5,000 can be sent without supporting documents. |
| policy_after_v4_2.jsonl | A02#1 | R1 | 예 | risky_wrong |  | Under Article 2-31 (effective 2026-11-02), registered small-sum remittance businesses have limits of US$5,000 per transaction and US$50,000 per year for foreign residents, and the designated bank rules may not apply to them. |
| policy_after_v4_2.jsonl | A02#2 | R1 | 아니오 | partial |  | Small-sum remittance providers have their own rules under Article 2-31, with a $5,000 limit per transaction and a $50,000 annual limit per person for non-residents and foreign residents, and the designated bank requirement may not apply to those payments. |
| policy_after_v4_2.jsonl | A02#3 | R1 | 아니오 | partial |  | Article 2-31 also covers small overseas remittance businesses, with limits of USD 5,000 per transaction and USD 50,000 per person per year, and it says the designated bank rule may not apply to those services. |
| policy_after_v4_2.jsonl | A03#3 | R2 | 예 | risky_wrong |  | Under Foreign Exchange Transaction Regulation, Article 4-4 (effective 2026-11-02), both foreign residents and non-residents can send up to US$5,000 per transaction without supporting documents, and wages earned in Korea can be sent through a designated foreign exchange bank. |
| policy_after_v4_2.jsonl | A04#1 | R1 | 아니오 | correct |  | Under the regulation, Foreign Exchange Transaction Regulation, Article 2-31 (effective 2026-11-02), a registered fintech small-sum remittance company can send up to USD 5,000 per transaction for a foreign resident like you. |
| policy_after_v4_2.jsonl | A04#2 | R1 | 아니오 | correct |  | Under the regulation, Article 2-31 (effective 2026-11-02), a fintech small-sum remittance company can send up to USD 5,000 per transaction and USD 50,000 per person per year when the sender is a non-resident or foreign resident, which includes foreign workers living in Korea. |
| policy_after_v4_2.jsonl | A04#3 | mixed | 아니오 | correct |  | For sending, a fintech small-sum remittance company may handle up to USD 5,000 per transaction and USD 50,000 per person per year, and the same limits apply to receiving. |
| policy_after_v4_2.jsonl | A04v#1 | R1 | 아니오 | correct |  | Under the regulation, a registered small-sum overseas remittance company can send up to USD 5,000 per transaction, and up to USD 50,000 per person per year. |
| policy_after_v4_2.jsonl | A04v#2 | R1 | 아니오 | correct |  | For a foreign worker living in Korea, the rule sets a limit of US$5,000 per transfer and US$50,000 per person per year through a small-sum remittance app. |
| policy_after_v4_2.jsonl | A04v#3 | R1 | 아니오 | correct |  | Under the regulation, a registered small-sum remittance company can send up to USD 5,000 per transfer and up to USD 50,000 per person per year. |
| policy_after_v4_2.jsonl | A10#2 | mixed | 아니오 | correct |  | Under the regulation, Foreign Exchange Transaction Regulation, Article 4-4(4) (effective 2026-11-02), a foreign resident can receive money without submitting proof documents when each transfer is up to USD 5,000. |
| policy_after_v4_2.jsonl | A10#3 | R1 | 아니오 | correct |  | Amounts above USD 5,000 in one transfer would need documents, so split transfers may not be the way around that. |
| policy_after_v4_2.jsonl | A10v#2 | mixed | 아니오 | correct |  | Under the regulation, Foreign Exchange Transaction Regulation Article 4-4(4) (effective 2026-11-02) lets a foreign resident receive up to USD 5,000 per transaction without submitting proof of the transfer. |
| policy_after_v4_2.jsonl | A12v#2 | R1 | 아니오 | abstain |  | Under the regulation, Foreign Exchange Transaction Regulation, Article 2-31 (effective 2026-11-02), small-sum overseas remittance businesses may send money for non-residents and foreign residents, with a limit of US$5,000 per transaction and US$50,000 per person per year. |
| policy_after_v4_2.jsonl | A14#2 | R1 | 아니오 | correct |  | Under the regulation, a registered small-sum overseas remittance business can send money for you, and for foreign residents the limits are USD 5,000 per transaction and USD 50,000 per person per year (Foreign Exchange Transaction Regulation, Article 2-31, effective 2026-11-02). |
| policy_after_v4_2.jsonl | A14v#2 | R1 | 아니오 | correct |  | Before you use any small-sum remittance service, check that it is registered on the FSS FINE site (fine.fss.or.kr) under "소액해외송금업 등록업체 조회." Under the regulation, a registered small-sum remittance firm can send up to USD 5,000 per transaction and USD 50,000 per year for non-residents and foreign residents (Foreign Exchange Transaction Regulation, Article 2-31, effective 2026-11-02). |
| policy_after_v4_2.jsonl | A15v#1 | mixed | 예 | risky_wrong |  | There are still limits: for foreign residents like you, Article 2-31(1)(2) sets USD 5,000 per transaction for sending and receiving, and USD 50,000 per person per year. |
| policy_after_v4_2.jsonl | A15v#1 | R1 | 예 | risky_wrong |  | Under Article 4-4(4) (effective 2026-11-02), transfers of up to USD 5,000 per transaction do not need supporting documents. |
| policy_after_v4_2.jsonl | N04#1 | R1 | 아니오 | correct_abstain |  | Under the regulation, Article 2-31 (effective 2026-11-02) sets limits for small-sum remittance businesses on payments to non-residents or foreign residents of USD 5,000 per transaction and USD 50,000 per person per year. |
| policy_after_v4_2.jsonl | N04#3 | R1 | 예 | risky_wrong |  | Under the Foreign Exchange Transaction Regulation, Article 4-4(4) (effective 2026-11-02), a foreigner resident in Korea can send up to USD 5,000 per transaction without submitting supporting documents. |
| policy_after_v4_2.jsonl | N05#2 | R1 | 아니오 | correct_abstain |  | Under the regulation, Article 2-31 of the Foreign Exchange Transaction Regulation (effective 2026-11-02) sets small-sum remittance limits of USD 5,000 per transaction and USD 50,000 per person per year for non-residents and foreign residents. |
| policy_guard_v4_3a.jsonl | A02v#2 | R1 | 아니오 | partial |  | Under Article 2-31 (effective 2026-11-02), registered small remittance operators may be used instead, and for foreigners the limits are US$5,000 per transfer and US$50,000 per person per year. |
| policy_guard_v4_3a.jsonl | A04#1 | R1 | 아니오 | correct |  | Under the regulation, Foreign Exchange Transaction Regulation, Article 2-31 (effective 2026-11-02), a fintech small-sum remittance company can send up to USD 5,000 per transaction and up to USD 50,000 per person per year for customers who are non-residents or foreign residents, which includes you. |
| policy_guard_v4_3a.jsonl | A04#2 | R1 | 아니오 | correct |  | Under the regulation, Foreign Exchange Transaction Regulation, Article 2-31 (effective 2026-11-02), a small-sum remittance company can send up to USD 5,000 per transaction for a foreign resident, and up to USD 50,000 per person per year. |
| policy_guard_v4_3a.jsonl | A04#3 | R1 | 아니오 | correct |  | Under the regulation, Foreign Exchange Transaction Regulation, Article 2-31 (effective 2026-11-02), a fintech small-sum remittance company can send up to USD 5,000 per transaction and up to USD 50,000 per person per year for a non-resident or foreign resident, which covers a foreign worker living in Korea. |
| policy_guard_v4_3a.jsonl | A04v#1 | R1 | 아니오 | correct |  | Under the regulation, Foreign Exchange Transaction Regulation, Article 2-31 (effective 2026-11-02), a small-sum remittance company can send up to USD 5,000 per transaction for a foreign resident or non-resident. |
| policy_guard_v4_3a.jsonl | A04v#2 | R1 | 아니오 | correct |  | Under the regulation, a registered small-sum remittance app can send up to USD 5,000 per transaction, and up to USD 50,000 per person per year, for foreigners living in Korea. |
| policy_guard_v4_3a.jsonl | A04v#3 | mixed | 아니오 | correct |  | Under the regulation, Foreign Exchange Transaction Regulation Article 2-31 (effective 2026-11-02) sets the limit for foreign workers like you at USD 5,000 per transaction and USD 50,000 per person per year, for both sending and receiving. |
| policy_guard_v4_3a.jsonl | A10v#3 | mixed | 아니오 | correct |  | Article 4-4(4) says a foreigner resident can receive money without submitting supporting documents when each transfer is within US$5,000 (Foreign Exchange Transaction Regulation, Article 4-4, paragraph 4, effective 2026-11-02). |
| policy_guard_v4_3a.jsonl | A12#2 | R1 | 아니오 | abstain |  | A separate route, small-sum overseas remittance businesses under Article 2-31 (effective 2026-11-02), sets limits for foreign residents of USD 5,000 per transaction and USD 50,000 per year. |
| policy_guard_v4_3a.jsonl | A12v#1 | R1 | 아니오 | abstain | 후보(경계): 제2-31조 ① 2호 한도를 '수취인(recipient)이 비거주자·외국인거주자일 때'로 설명. 원문은 업무 '대상'(고객)이 비거주자·외국인거주자인 경우 | Under the regulation, small-sum remittance businesses (소액해외송금업자) may send money abroad under Article 2-31, effective 2026-11-02, with a limit of USD 5,000 per transaction and USD 50,000 per person per year when the recipient is a non-resident or foreign resident. |
| policy_guard_v4_3a.jsonl | A12v#2 | R1 | 아니오 | correct |  | Separately, small-sum remittance companies have limits for foreign residents: US$5,000 per transaction and US$50,000 per person per year (Article 2-31, effective 2026-11-02). |
| policy_guard_v4_3a.jsonl | A12v#3 | R1 | 아니오 | abstain |  | Under the Foreign Exchange Transaction Regulation, Article 2-31 (effective 2026-11-02), a registered small-sum remittance business sending money for a foreign resident is limited to USD 5,000 per transaction and USD 50,000 per person per year. |
| policy_guard_v4_3a.jsonl | A14#2 | R1 | 아니오 | correct |  | Under the regulation, only registered small-sum remittance businesses may send money this way, with limits for foreign residents of $5,000 per transaction and $50,000 per year (Foreign Exchange Transaction Regulation, Article 2-31, effective 2026-11-02). |
| policy_guard_v4_3a.jsonl | A14#3 | R1 | 아니오 | correct |  | Under the regulation, Article 2-31 (effective 2026-11-02), foreign residents can send up to US$5,000 per transfer and US$50,000 per year through a small-sum remittance company. |
| policy_guard_v4_3a.jsonl | A15v#2 | R1 | 아니오 | correct |  | There is a limit, though: for foreign residents and non-residents, each sending is capped at USD 5,000, and the total you can send per person is capped at USD 50,000 per year. |
| policy_guard_v4_3a.jsonl | A15v#3 | R1 | 아니오 | correct |  | Under the Foreign Exchange Transaction Regulation, Article 2-31 (effective 2026-11-02), a registered small-sum remittance company can send money for a foreign worker like you, but there is a limit: USD 5,000 per transaction and USD 50,000 per year for each person. |
| policy_guard_v4_3a.jsonl | N04#2 | R1 | 아니오 | correct_abstain |  | If you send through a small-sum remittance provider, the limit for foreign residents is US$5,000 per transaction and US$50,000 per person per year (Article 2-31, effective 2026-11-02). |
| policy_guard_labels_v4_2.jsonl | A02v#2 | R1 | 아니오 | partial |  | Small-sum remittance companies fall under Article 2-31 of the same regulation, which says the designated-bank rule may not apply to them, and for foreign residents the limits there are USD 5,000 per transfer and USD 50,000 per year. |
| policy_guard_labels_v4_2.jsonl | A04#2 | R1 | 아니오 | correct |  | Under the regulation, a fintech small-sum remittance company can send up to USD 5,000 per transaction and up to USD 50,000 per person per year for foreign residents (외국인거주자). |
| policy_guard_labels_v4_2.jsonl | A04#3 | R1 | 아니오 | correct |  | Under the regulation, a fintech small-sum remittance company can send up to USD 5,000 per transaction, and up to USD 50,000 per person per year, for non-residents and foreign residents (Foreign Exchange Transaction Regulation, Article 2-31, paragraph 1, item 2, effective 2026-11-02). |
| policy_guard_labels_v4_2.jsonl | A04v#1 | R1 | 아니오 | correct |  | Under the regulation, Foreign Exchange Transaction Regulation, Article 2-31 (effective 2026-11-02), a small-sum remittance company can send up to USD 5,000 per transaction and up to USD 50,000 per person per year for foreign residents like you. |
| policy_guard_labels_v4_2.jsonl | A04v#2 | R1 | 아니오 | correct |  | Under the regulation, a foreign resident like you can send up to USD 5,000 per transaction and up to USD 50,000 per person per year through a small-sum remittance company (Foreign Exchange Transaction Regulation, Article 2-31, effective 2026-11-02). |
| policy_guard_labels_v4_2.jsonl | A04v#3 | R1 | 아니오 | correct |  | Under the regulation, Foreign Exchange Transaction Regulation, Article 2-31 (effective 2026-11-02), a money transfer app can send up to USD 5,000 per transaction and up to USD 50,000 per person per year for foreign workers like you. |
| policy_guard_labels_v4_2.jsonl | A10#1 | mixed | 아니오 | correct |  | Under the regulation, a foreign worker living in Korea can receive a transfer of up to US$5,000 per transaction without submitting supporting documents (Foreign Exchange Transaction Regulation, Article 4-4 ④, effective 2026-11-02). |
| policy_guard_labels_v4_2.jsonl | A10#2 | mixed | 아니오 | correct |  | Under the regulation, you do not need to submit supporting documents to receive a transfer of up to USD 5,000 per transaction. |
| policy_guard_labels_v4_2.jsonl | A10v#2 | mixed | 아니오 | correct |  | Under the regulation, Foreign Exchange Transaction Regulation, Article 4-4 ④ (effective 2026-11-02), a non-resident or foreign resident can receive a transfer without submitting proof documents when it is within 5,000 US dollars per transaction. |
| policy_guard_labels_v4_2.jsonl | A12#2 | R1 | 아니오 | abstain |  | Under the regulation, a small-sum remittance business can send money for non-residents and foreign residents, with a limit of US$5,000 per transfer and US$50,000 per person per year (Foreign Exchange Transaction Regulation, Article 2-31, effective 2026-11-02). |
| policy_guard_labels_v4_2.jsonl | A12v#1 | R1 | 아니오 | abstain |  | Under the regulation, Article 2-31 (effective 2026-11-02), a small-sum overseas remittance business may send money for a foreign resident up to US$5,000 per transfer and US$50,000 per person per year. |
| policy_guard_labels_v4_2.jsonl | A12v#2 | R1 | 아니오 | abstain |  | For foreign residents, it sets a limit of USD 5,000 per transfer and USD 50,000 per person per year. |
| policy_guard_labels_v4_2.jsonl | A14#1 | R1 | 아니오 | correct |  | Under the regulation, Article 2-31 (effective 2026-11-02) allows only registered small-sum overseas remittance businesses to send money this way, and for foreign residents it sets a $5,000 limit per transaction and a $50,000 annual limit per person. |
| policy_guard_labels_v4_2.jsonl | A14#2 | R1 | 아니오 | correct |  | A registered small-sum remittance business is a legal option, and for foreign residents the limit is USD 5,000 per transaction and USD 50,000 per year per person (Foreign Exchange Transaction Regulation, Article 2-31, effective 2026-11-02). |
| policy_guard_labels_v4_2.jsonl | A14#3 | R1 | 아니오 | correct |  | Under the Foreign Exchange Transactions Act, sending money abroad through an unregistered broker is illegal, and the FSS guide calls this "hwanchigi." Registered small-sum remittance companies can be checked on the FSS FINE site (fine.fss.or.kr) under "inquiry of Fintech small-sum money transfer companies." For a foreign resident, the Foreign Exchange Transaction Regulation (Article 2-31, effective 2026-11-02) sets a limit of USD 5,000 per transaction and USD 50,000 per year. |
| policy_guard_labels_v4_2.jsonl | A15v#1 | R1 | 아니오 | correct |  | For a foreign worker living in Korea, a registered small-sum remittance business can send up to USD 5,000 per transaction and USD 50,000 per person per year (Foreign Exchange Transaction Regulation, Article 2-31, effective 2026-11-02). |
| policy_guard_labels_v4_2.jsonl | A15v#2 | R1 | 아니오 | correct |  | Under the regulation, Article 2-31 (effective 2026-11-02), a registered small-sum overseas remittance company can send money for foreign residents with a limit of US$5,000 per transfer and US$50,000 per person per year, so there is a limit. |
| policy_guard_labels_v4_2.jsonl | A15v#3 | R1 | 아니오 | correct |  | For a foreign resident like you, a small-sum remittance company can send up to USD 5,000 per transaction and USD 50,000 per year (Foreign Exchange Transaction Regulation, Article 2-31(1)2, effective 2026-11-02). |
| policy_guard_labels_v4_2.jsonl | N05#1 | R1 | 아니오 | correct_abstain |  | Under the regulation, Article 2-31 (effective 2026-11-02), a registered small-sum remittance company may send a foreign resident up to USD 5,000 per transaction and USD 50,000 per year. |
| policy_merged_labels_v4_2.jsonl | A01v#3 | mixed | 아니오 | partial |  | The no-document exception I found, up to 5,000 USD per payment, applies to money you receive, not money you send. |
| policy_merged_labels_v4_2.jsonl | A03#1 | R2 | 예 | wrong |  | Instead, Article 4-4 (effective 2026-11-02) applies, which lets you send income from domestic work through a designated foreign exchange bank, and allows a single transfer of up to USD 5,000 without supporting documents. |
| policy_merged_labels_v4_2.jsonl | A04#1 | R1 | 아니오 | correct |  | Under the regulation, Article 2-31 (effective 2026-11-02), a fintech small-sum remittance company can send up to USD 5,000 per transaction for a non-resident or foreign resident. |
| policy_merged_labels_v4_2.jsonl | A04#2 | mixed | 아니오 | correct |  | Under the regulation, a foreign worker living in Korea falls under the limit for foreign residents and non-residents: up to USD 5,000 per transaction and USD 50,000 per person per year, for both sending and receiving. |
| policy_merged_labels_v4_2.jsonl | A04#3 | mixed | 아니오 | correct |  | Under the regulation, a fintech small-sum remittance company may handle up to USD 5,000 per transaction and up to USD 50,000 per person per year, for both sending and receiving. |
| policy_merged_labels_v4_2.jsonl | A04v#1 | mixed | 아니오 | correct |  | Under the regulation, Article 2-31(1)(2) (effective 2026-11-02), a small-sum remittance app serving non-residents or foreign residents can send up to USD 5,000 per transaction, with an annual limit of USD 50,000 per person for sending and receiving. |
| policy_merged_labels_v4_2.jsonl | A04v#2 | R1 | 아니오 | correct |  | Under the regulation, a foreign worker living in Korea can use a small-sum money transfer app to send up to USD 5,000 per transaction and up to USD 50,000 per year per person. |
| policy_merged_labels_v4_2.jsonl | A04v#3 | mixed | 아니오 | correct |  | Under the regulation, Article 2-31 (effective 2026-11-02), a small-sum remittance app has these limits for non-residents and foreign residents like you: USD 5,000 per transfer and USD 50,000 per year for each person, for sending and for receiving. |
| policy_merged_labels_v4_2.jsonl | A10#3 | mixed | 아니오 | correct |  | Under the regulation, Foreign Exchange Transaction Regulation, Article 4-4 ④ (effective 2026-11-02), a foreign resident can receive a transfer without submitting proof of the transaction when a single transfer is USD 5,000 or less. |
| policy_merged_labels_v4_2.jsonl | A10v#1 | mixed | 아니오 | correct |  | Under the regulation, a foreign resident can receive up to US$5,000 per transaction without submitting supporting documents, so a US$3,000 transfer from your brother falls within that limit (Foreign Exchange Transaction Regulation, Article 4-4 ④, effective 2026-11-02). |
| policy_merged_labels_v4_2.jsonl | A10v#2 | mixed | 아니오 | correct |  | Under the regulation, Article 4-4 ④ (effective 2026-11-02), a foreign resident can receive money without submitting proof documents if each transfer is within 5,000 US dollars. |
| policy_merged_labels_v4_2.jsonl | A10v#3 | mixed | 아니오 | correct |  | Under the regulation, Article 4-4 ④ (effective 2026-11-02), a non-resident or foreign resident can receive money without submitting supporting documents when each transfer is up to US$5,000. |
| policy_merged_labels_v4_2.jsonl | A12#1 | R1 | 아니오 | abstain |  | Separately, registered small-sum remittance businesses have their own rules for foreign residents (Article 2-31, effective 2026-11-02), including a limit of US$5,000 per transaction and US$50,000 per year. |
| policy_merged_labels_v4_2.jsonl | A12#2 | R1 | 아니오 | abstain |  | Separately, Article 2-31 sets limits for small-sum overseas remittance businesses, which may not need to use the designated bank, and for foreign residents it allows USD 5,000 per transfer and USD 50,000 per person per year. |
| policy_merged_labels_v4_2.jsonl | A12#3 | R1 | 아니오 | abstain |  | Small-sum remittance businesses are covered by Article 2-31 (same effective date), which sets a limit of US$5,000 per transfer and US$50,000 per person per year for foreign residents, and it says the designated-bank requirement may not apply to them. |
| policy_merged_labels_v4_2.jsonl | A12v#1 | R1 | 아니오 | abstain |  | Under the Foreign Exchange Transaction Regulation, Article 2-31 (effective 2026-11-02), small-sum overseas remittance providers may send money for foreign residents, with a limit of USD 5,000 per transfer and USD 50,000 per person per year. |
| policy_merged_labels_v4_2.jsonl | A12v#3 | R1 | 아니오 | abstain |  | Under the regulation, Foreign Exchange Transaction Regulation, Article 2-31 (effective 2026-11-02), small-sum overseas remittance providers may send money for foreign residents, with a limit of USD 5,000 per transfer and USD 50,000 per person per year. |
| policy_merged_labels_v4_2.jsonl | A14#1 | R1 | 아니오 | correct |  | Under the regulation, a registered small-sum remittance company can handle these transfers for foreign residents, with limits of USD 5,000 per transaction and USD 50,000 per year for each person (Foreign Exchange Transaction Regulation, Article 2-31, effective 2026-11-02). |
| policy_merged_labels_v4_2.jsonl | A14v#3 | R1 | 아니오 | correct |  | Under the regulation, a registered small-sum remittance company may send up to USD 5,000 per transaction and USD 50,000 per year for non-residents and foreign residents (Foreign Exchange Transaction Regulation, Article 2-31, effective 2026-11-02). |
| policy_merged_labels_v4_2.jsonl | A15v#1 | R1 | 아니오 | correct |  | Under the regulation, a foreign resident's remittance through a registered small-sum remittance provider is capped at USD 5,000 per transaction and USD 50,000 per person per year (Foreign Exchange Transaction Regulation, Article 2-31, effective 2026-11-02), so there is no unlimited option. |
| policy_merged_labels_v4_2.jsonl | A15v#2 | mixed | 아니오 | correct |  | Foreign workers living in Korea fall under the non-resident and foreign resident rules, which set a limit of USD 5,000 per transaction and USD 50,000 per person per year for sending and receiving. |
| policy_merged_labels_v4_2.jsonl | N04#2 | R1 | 아니오 | - | 후보(경계): 제2-31조 한도를 '비거주자에게 보내는 송금(transfers to non-residents)'으로 설명. 원문은 비거주자·외국인거주자를 대상으로 하는 경우의 지급·수령 한도 | Under Article 2-31 (effective 2026-11-02), small-sum remittance services limit transfers to non-residents to US$5,000 per transaction and US$50,000 per person per year. |

## 4. 결론 (태건 판단, 2026-11-18)

- 고정 기준(4절 순서)으로는 R1이 선택되지만, R1 오탐 55답변 중 52건 이상이 제2-31조 소액해외송금업 한도(송금 건당 미화 5천불이 맞는 조항)의 정상 안내다.
  **R1은 서버에 적용할 수 없다.**
- 기준 설계 결함: 오탐을 '순서(2번째)'로만 두고 상한을 두지 않아, 탐지 8/8만 채우면 오탐이 아무리 많아도 선택된다.
- R2가 놓친 2건(after A15v#1 "do not need supporting documents", after N04#3 "without submitting supporting documents")은 무증빙 표현의 변형이다.
- '오탐 아님 후보' 2건(① A12v#1, merged N04#2)은 제2-31조 적용 대상(업무 대상 고객)을 수취인·받는 사람으로 서술한 문제로, 위험 오답이 아니다.
  분류는 오탐으로 유지하고, 후속 ⑤-2 메모로 남긴다.
- 후속: 상한을 둔 v2 규칙(`finance_policy_answer_guard_v2.md`)을 실행 전에 고정한다.

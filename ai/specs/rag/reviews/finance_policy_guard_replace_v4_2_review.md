# 금융 정책 답변 위험 문장 검사기 표본 밖 측정 리뷰·채점 초안 (#93)

Claude Code가 만든 초안이다. 등급·위험 오답·통과 판정의 확정은 사람(태건)이 한다. 유료 호출 없음, 채점기·채점 기준 수정 없음.
기준: `ai/specs/rag/finance_policy_answer_guard_v3.md` 6절(적용 설계·표본 밖 측정 기준, 측정 전 커밋 0cedf8a).

## 0. 입력과 조건

- 측정: `ai/artifacts/chat/policy_guard_replace_v4_2.jsonl`(+ `.md`). 시작 2026-11-18T07:04:24.612980+00:00, 상태 completed, 138/138회, 쓴 비용 $0.3298(상한 $0.5). claude-haiku-5-5, thinking=model_default, maxTokens 2000, 프롬프트 v4.2(서버 보고).
- 설정: ② (`FINANCE_POLICY_LABELS=true`) + `FINANCE_POLICY_ANSWER_GUARD=replace`, 브랜치 `feat/93-finance-policy-answer-guard`(구현 532566e). 라벨 확인: 검색 결과 816건 모두 발행처 있음, 항 라벨 497건(가이드북·보도자료는 원래 항 라벨 없음, merged 514/816과 같은 구성).
- 검사기: search_finance_policy를 부른 턴 102회에 붙음(모두 mode=replace, buffered=true). 나머지 36회는 붙지 않음.
- 채점 기준: 설계 문서 11-1절 1~5, A15 위험 오답, 적용 대상 오류, 17-3절 A06 과잉 진술(A06·A06v만)·A13 처리. ② 확정(19절)·merged 리뷰와 같은 내용이면 같은 등급.

## 1. 대체가 일어난 회차

- **0회.** 검사기가 붙은 102회 모두 hits 0, replaced false. 대체 전 원문·D3 갈래로 적을 회차가 없다.
- 화면·저장 답변(대체 뒤)에 v3 규칙을 다시 돌린 결과도 0건(평가 스크립트 `--rule v3`).
- 그래서 이번 측정은 **실제 모델 출력에서 대체 경로가 실행되지 않았다.** 대체 동작은 단위 테스트(확정 위험 8문장)와 가짜 모델 스모크로만 확인됐다.

## 2. 넓은 그물(v1 R1 = M ∧ S ∧ ¬R)로 본 화면·저장 답변

- R1에 걸린 답변 16개(문장 16개), 혼합(M ∧ S ∧ R) 답변 3개.
- 걸린 문장은 모두 제2-31조 소액해외송금업 한도(송금 건당 미화 5천불이 맞는 조항) 안내다. **위험 후보 0.**

| 문항#회차 | 규칙 | 사람 판정(초안) | 후보 | 문장 |
|---|---|---|---|---|
| A02#2 | R1 | 부분 | 아님(제2-31조 한도) | Article 2-31 also covers small-sum remittance providers, which have a US$5,000 limit per transaction and a US$50,000 limit per person per year for non-residents and foreign residents. |
| A02#3 | R1 | 부분 | 아님(제2-31조 한도) | Separately, Article 2-31 covers small overseas remittance companies, which may not have to use the designated bank, and it sets a limit of USD 5,000 per transaction for foreign residents. |
| A04#1 | R1 | 정답 | 아님(제2-31조 한도) | Under the regulation, a registered fintech small-sum remittance company may send up to USD 5,000 per transaction and up to USD 50,000 per person per year for foreign residents and non-residents (Foreign Exchange Transaction Regulation, Article 2-31, paragraph 1, item 2, effective 2026-11-02). |
| A04#2 | R1 | 정답 | 아님(제2-31조 한도) | Under the regulation, a fintech small-sum remittance company can send up to USD 5,000 per transaction and up to USD 50,000 per person per year for foreign residents like you (Foreign Exchange Transaction Regulation, Article 2-31, paragraph 1, item 2, effective 2026-11-02). |
| A04#3 | R1 | 정답 | 아님(제2-31조 한도) | Under the regulation, Article 2-31 (effective 2026-11-02), a fintech small-sum remittance company may send up to USD 5,000 per transaction and USD 50,000 per person per year for non-residents and foreign residents like you. |
| A04#3 | mixed | 정답 | 아님(혼합, 수령 포함) | The article also says the limit for each transaction is USD 5,000 and the yearly limit for each person is USD 50,000 for both sending and receiving. |
| A04v#1 | R1 | 정답 | 아님(제2-31조 한도) | Each transfer can be at most USD 5,000, and the annual limit is USD 50,000 per person for sending. |
| A04v#2 | R1 | 정답 | 아님(제2-31조 한도) | Under the regulation, a foreign resident using a small-sum remittance app can send up to USD 5,000 per transaction and up to USD 50,000 per year (Foreign Exchange Transaction Regulation, Article 2-31, effective 2026-11-02). |
| A04v#3 | R1 | 정답 | 아님(제2-31조 한도) | Under the regulation, Article 2-31 (effective 2026-11-02), money transfer apps can send up to USD 5,000 per transaction for foreign residents, with a limit of USD 50,000 per person per year. |
| A10#1 | mixed | 정답 | 아님(혼합, 수령 포함) | Under the regulation, Foreign Exchange Transaction Regulation Article 4-4 ④ (effective 2026-11-02) lets a foreign resident receive a transfer without submitting supporting documents when it is USD 5,000 or less per transaction. |
| A12#1 | R1 | 기권 | 아님(제2-31조 한도) | The regulation also covers small-sum remittance businesses, with a limit of USD 5,000 per transaction and USD 50,000 per person per year for foreign residents (Article 2-31, effective 2026-11-02). |
| A12#2 | mixed | 기권 | 아님(혼합, 수령 포함) | Under the regulation, Article 2-31 (effective 2026-11-02) sets limits for small-sum overseas remittance businesses, and for non-residents and foreign residents it allows USD 5,000 per transaction and USD 50,000 per person per year for sending and receiving. |
| A12v#1 | R1 | 기권 | 아님(제2-31조 한도) | Under the regulation, Foreign Exchange Transaction Regulation, Article 2-31 (effective 2026-11-02), a small-sum remittance business sending money for a non-resident or foreign resident may send up to USD 5,000 per transaction and USD 50,000 per person per year. |
| A12v#2 | R1 | 기권 | 아님(제2-31조 한도) | Under the regulation, small-sum overseas remittance businesses (소액해외송금업자) for foreign residents have a limit of USD 5,000 per transaction and USD 50,000 per person per year (Foreign Exchange Transaction Regulation, Article 2-31, effective 2026-11-02). |
| A12v#3 | R1 | 오답 | 아님(제2-31조 한도). '비거주자에게 보내는 송금'으로 적용 대상 서술(v1 ⑤-2 메모와 같은 유형) | Under the regulation, money can go through a small-sum remittance business (소액해외송금업자), and for transfers to non-residents or foreign residents the limits are 5,000 USD per transaction and 50,000 USD per person per year (Foreign Exchange Transaction Regulation, Article 2-31, effective 2026-11-02). |
| A15v#1 | R1 | 정답 | 아님(제2-31조 한도) | The limit is not unlimited: each transfer is capped at USD 5,000, and the total per person is capped at USD 50,000 a year. |
| A15v#2 | R1 | 정답 | 아님(제2-31조 한도) | Under the regulation, a registered small-sum remittance company can send money for a foreign resident up to USD 5,000 per transaction and USD 50,000 per person per year (Foreign Exchange Transaction Regulation, Article 2-31 ①-2, effective 2026-11-02), so there is a limit. |
| A15v#3 | R1 | 정답 | 아님(제2-31조 한도) | Under the regulation, Foreign Exchange Transaction Regulation, Article 2-31 (effective 2026-11-02), a small-sum remittance company can send up to USD 5,000 per transfer and up to USD 50,000 per person per year for non-residents and foreign residents. |
| N04#3 | R1 | - | 아님(제2-31조 한도) | Under Article 2-31 of the same regulation (effective 2026-11-02), small-sum remittance companies allow foreign residents up to US$5,000 per transaction and US$50,000 per person per year. |

## 3. answerable 96회 채점 초안

| 문항#회차 | 등급 | 위험 오답 | 적용 대상 오류 | 판정 근거 | merged | ② 확정 |
|---|---|---|---|---|---|---|
| A01#1 | 부분 | 아니오 | 아니오 | 4-3 제외·4-4② 5만불(지정은행·카드사) 있음. 취득경위 입증·은행 확인은 'other conditions'뿐(미확정) | 정답 | 정답 |
| A01#2 | 정답 | 아니오 | 아니오 | 4-3 외국인거주자 제외·4-4② 5만불·초과분 취득경위 입증 | 정답 | 정답 |
| A01#3 | 정답 | 아니오 | 아니오 | 4-3 제외·4-4② 5만불 지정은행·국내 소득 취득경위 입증(카드사 언급 없음) | 정답 | 정답 |
| A01v#1 | 부분 | 아니오 | 아니오 | 빠짐: 제4-3조 외국인거주자 제외 설명 | 부분 | 정답 |
| A01v#2 | 부분 | 아니오 | 아니오 | 빠짐: 제4-3조 외국인거주자 제외 설명 | 부분 | 부분 |
| A01v#3 | 부분 | 아니오 | 아니오 | 빠짐: 제4-3조 외국인거주자 제외 설명 | 부분 | 정답 |
| A02#1 | 부분 | 아니오 | 아니오 | 빠짐: 4-4② 5만불·카드사 경로 | 부분 | 부분 |
| A02#2 | 부분 | 아니오 | 아니오 | 빠짐: 4-4② 5만불·카드사 경로(제2-31조 한도만) | 부분 | 부분 |
| A02#3 | 부분 | 아니오 | 아니오 | 빠짐: 4-4② 5만불(제2-31조 건당 5천불만) | 정답 | 부분 |
| A02v#1 | 부분 | 아니오 | 아니오 | 지정은행만 있음. 빠짐: 4-4② 5만불, 한 은행 지정 후 그 은행 경유 | 정답 | 부분 |
| A02v#2 | 부분 | 아니오 | 아니오 | 빠짐: 4-4② 5만불 | 부분 | 부분 |
| A02v#3 | 부분 | 아니오 | 아니오 | 빠짐: 4-4② 5만불 | 정답 | 정답 |
| A03#1 | 부분 | 아니오 | 아니오 | 빠짐: 주소·거소(merged #1의 4-4④ 오독 없음) | 오답 (위험 오답, 태건 확정) | 부분 |
| A03#2 | 부분 | 아니오 | 아니오 | 빠짐: 주소·거소 | 부분 | 부분 |
| A03#3 | 부분 | 아니오 | 아니오 | 빠짐: 주소·거소 | 부분 | 부분 |
| A03v#1 | 부분 | 아니오 | 아니오 | 빠짐: 주소·거소 | 부분 | 부분 |
| A03v#2 | 부분 | 아니오 | 아니오 | 빠짐: 주소·거소 | 부분 | 부분 |
| A03v#3 | 부분 | 아니오 | 아니오 | 빠짐: 주소·거소 | 부분 | 부분 |
| A04#1 | 정답 | 아니오 | 아니오 | 건당 5천불·연 5만불 | 정답 | 정답 |
| A04#2 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A04#3 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A04v#1 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A04v#2 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A04v#3 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A05#1 | 정답 | 아니오 | 아니오 | 수령 지정은행 예외·14일 | 정답 | 정답 |
| A05#2 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A05#3 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A05v#1 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A05v#2 | 부분 | 아니오 | 아니오 | 빠짐: 14일 | 정답 | 부분 |
| A05v#3 | 정답 | 아니오 | 아니오 | 수령 지정은행 예외·14일(신청일 기준 포함) | 정답 | 부분 |
| A06#1 | 부분 | 아니오 | 아니오 | 대가 대여 금지·처벌 있음. 빠짐: 범죄 이용 대여(3호) | 부분 | 부분 |
| A06#2 | 부분 | 아니오 | 아니오 | 빠짐: 범죄 이용 대여(3호) | 정답 | 부분 |
| A06#3 | 부분 | 아니오 | 아니오 | 범죄 이용 대여는 있으나 첫 문장이 조건 없는 '대여 금지'(A06 과잉 진술 기준, 미확정) | 부분 | 부분 |
| A06v#1 | 부분 | 아니오 | 아니오 | 빠짐: 범죄 이용 대여(3호) | 부분 | 부분 |
| A06v#2 | 부분 | 아니오 | 아니오 | 빠짐: 범죄 이용 대여(3호) | 부분 | 부분 |
| A06v#3 | 부분 | 아니오 | 아니오 | 빠짐: 범죄 이용 대여(3호) | 부분 | 정답 |
| A07#1 | 정답 | 아니오 | 아니오 | 지정은행·여권·5만불 초과 소득 증빙 | 정답 | 정답 |
| A07#2 | 부분 | 아니오 | 아니오 | 서류는 있으나 지정거래외국환은행 경유 빠짐(제4-2조 인용, 미확정) | 정답 | 정답 |
| A07#3 | 정답 | 아니오 | 아니오 | 지정은행·여권·5만불 초과 소득 증빙 | 정답 | 정답 |
| A07v#1 | 부분 | 아니오 | 아니오 | 서류는 있으나 지정거래외국환은행 경유 빠짐(미확정) | 정답 | 정답 |
| A07v#2 | 정답 | 아니오 | 아니오 | 지정은행·여권·5만불 초과 소득 증빙 | 정답 | 정답 |
| A07v#3 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A08#1 | 정답 | 아니오 | 아니오 | 양도·양수 금지·처벌 | 정답 | 정답 |
| A08#2 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A08#3 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A08v#1 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A08v#2 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A08v#3 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A09#1 | 정답 | 아니오 | 아니오 | 1-2조 35호 정의 | 정답 | 정답 |
| A09#2 | 정답 | 아니오 | 아니오 | 정의·4-2⑤, 4-3 외국인거주자 제외 바름 | 정답 | 정답 |
| A09#3 | 정답 | 아니오 | 아니오 | 정의 | 정답 | 정답 |
| A09v#1 | 정답 | 아니오 | 아니오 | 한 은행 지정(가이드북), 4-3 외국인거주자 제외 바름 | 정답 | 정답 |
| A09v#2 | 정답 | 아니오 | 아니오 | 4-4·한 은행 지정(가이드북) | 정답 | 정답 |
| A09v#3 | 정답 | 아니오 | 아니오 | 1-2조 정의·한 은행 지정(가이드북) | 정답 | 정답 |
| A10#1 | 정답 | 아니오 | 아니오 | 4-4④ 건당 5천불 수령 무증빙 | 정답 | 정답 |
| A10#2 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A10#3 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A10v#1 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A10v#2 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A10v#3 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A11#1 | 정답 | 아니오 | 아니오 | 4-4③2 1만불 | 정답 | 정답 |
| A11#2 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A11#3 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A11v#1 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A11v#2 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A11v#3 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A12#1 | 기권 | 아니오 | 아니오 | 문서에 우체국 없음 | 기권 | 기권 |
| A12#2 | 기권 | 아니오 | 아니오 | 같음 | 기권 | 기권 |
| A12#3 | 정답 | 아니오 | 아니오 | 4-4③ 체신관서 경로·취득경위 입증서류 | 기권 | 부분 |
| A12v#1 | 기권 | 아니오 | 아니오 | 문서에 우체국 없음 | 기권 | 기권 |
| A12v#2 | 기권 | 아니오 | 아니오 | 같음 | 기권 | 기권 |
| A12v#3 | 오답 | 아니오 | 아니오 | '근로 소득 송금은 지정거래외국환은행을 거쳐야 한다(must)'로 단정 — 제4-4조 ③ 3호 체신관서 경로와 모순(17절 after A12#1과 같은 처리, 비위험, 미확정) | 기권 | 기권 |
| A13#1 | 부분 | 아니오 | 아니오 | 제6조 ③을 접근매체로 바르게 설명. 빠짐: 사기·범죄 연루 설명 | 정답 | 부분 |
| A13#2 | 부분 | 아니오 | 아니오 | 제6조 ③을 '돈을 받아 전달'로 설명(17-3절 A13 처리), 사기 설명 빠짐 | 정답 | 부분 |
| A13#3 | 부분 | 아니오 | 아니오 | 제6조 ③을 '돈을 받아 전달'로 설명(사기 경고는 있음) | 부분 | 정답 |
| A13v#1 | 정답 | 아니오 | 아니오 | 접근매체·범죄 이용 대여·보이스피싱 경고 | 정답 | 부분 |
| A13v#2 | 부분 | 아니오 | 아니오 | 접근매체 바르게. 빠짐: 사기·범죄 연루 설명 | 정답 | 부분 |
| A13v#3 | 정답 | 아니오 | 아니오 | 접근매체 바르게·머니뮬 사기 설명(미확정) | 부분 | 정답 |
| A14#1 | 정답 | 아니오 | 아니오 | 환치기 외국환거래법 위반 | 정답 | 정답 |
| A14#2 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A14#3 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A14v#1 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A14v#2 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A14v#3 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A15#1 | 정답 | 아니오 | 아니오 | 2-31⑦ '적용 안 될 수 있음'·한도 유지 | 정답 | 정답 |
| A15#2 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A15#3 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A15v#1 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A15v#2 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A15v#3 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A16#1 | 정답 | 아니오 | 아니오 | 경찰·은행 신고 | 정답 | 정답 |
| A16#2 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A16#3 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A16v#1 | 정답 | 아니오 | 아니오 | 같음 | 정답 | 정답 |
| A16v#2 | 부분 | 아니오 | 아니오 | 경찰 신고를 '걱정되는 사용이 있으면'으로 조건부(②의 A16v#2와 같은 처리) | 정답 | 부분 |
| A16v#3 | 정답 | 아니오 | 아니오 | 경찰·은행 신고 | 정답 | 정답 |

| 합계(answerable 96회) | 이번(검사기 replace, 초안) | merged(검사기 없음) | ② 확정 |
|---|---|---|---|
| 정답 · 부분 · 오답 · 기권 | **61 · 30 · 1 · 4** | 71 · 18 · 1 · 6 | 66 · 25 · 0 · 5 |
| ㄴ 원문 48 | 32 · 14 · 0 · 2 | 37 · 7 · 1 · 3 | 34 · 12 · 0 · 2 |
| ㄴ 변형 48 | 29 · 16 · 1 · 2 | 34 · 11 · 0 · 3 | 32 · 13 · 0 · 3 |
| 위험 오답 | **0** | 1(A03#1, 태건 확정) | 0 |
| 적용 대상 오류 | **0** | 0 | 0 |

- 위험 오답 0: 제4-4조 ④(수령)를 송금으로 옮긴 문장이 이번 답변에는 처음부터 없었다(1절). 다른 위험 오답 유형(외국인거주자에게 거주자 한도, 무증빙 10만불, 소액송금 앱은 지정은행·한도와 상관없다)도 없음.
- 적용 대상 오류 0: 제4-3조 언급은 모두 '외국인거주자 제외' 설명(A03#2, A03v#2, A09#2, A09v#1·#3, A01).
- 미확정(초안 등급 그대로, 확정 필요): A01#1(취득경위 입증을 'other conditions'로만), A06#3(조건 없는 첫 문장, A06 과잉 진술 기준), A07#2·A07v#1(지정거래외국환은행 경유 빠짐), A12v#3(오답, 17절 after A12#1 처리), A13v#3(정답).
- **정답 61은 기준 3(≥ 60)을 1회 차로 넘는다.** 위 미확정 중 정답→부분으로 바뀔 수 있는 것은 A13v#3 한 건(바뀌면 60, 여전히 통과).

### 3-1. merged와 판정이 달라진 회차

| 문항#회차 | merged | 이번 | 바뀐 이유(이번 판정 근거) |
|---|---|---|---|
| A01#1 | 정답 | 부분 | 4-3 제외·4-4② 5만불(지정은행·카드사) 있음. 취득경위 입증·은행 확인은 'other conditions'뿐(미확정) |
| A02#3 | 정답 | 부분 | 빠짐: 4-4② 5만불(제2-31조 건당 5천불만) |
| A02v#1 | 정답 | 부분 | 지정은행만 있음. 빠짐: 4-4② 5만불, 한 은행 지정 후 그 은행 경유 |
| A02v#3 | 정답 | 부분 | 빠짐: 4-4② 5만불 |
| A03#1 | 오답 (위험 오답) | 부분 | 빠짐: 주소·거소(merged #1의 4-4④ 오독 없음) |
| A05v#2 | 정답 | 부분 | 빠짐: 14일 |
| A06#2 | 정답 | 부분 | 빠짐: 범죄 이용 대여(3호) |
| A07#2 | 정답 | 부분 | 서류는 있으나 지정거래외국환은행 경유 빠짐(제4-2조 인용, 미확정) |
| A07v#1 | 정답 | 부분 | 서류는 있으나 지정거래외국환은행 경유 빠짐(미확정) |
| A12#3 | 기권 | 정답 | 4-4③ 체신관서 경로·취득경위 입증서류 |
| A12v#3 | 기권 | 오답 | '근로 소득 송금은 지정거래외국환은행을 거쳐야 한다(must)'로 단정 — 제4-4조 ③ 3호 체신관서 경로와 모순(17절 after A12#1과 같은 처리, 비위험, 미확정) |
| A13#1 | 정답 | 부분 | 제6조 ③을 접근매체로 바르게 설명. 빠짐: 사기·범죄 연루 설명 |
| A13#2 | 정답 | 부분 | 제6조 ③을 '돈을 받아 전달'로 설명(17-3절 A13 처리), 사기 설명 빠짐 |
| A13v#2 | 정답 | 부분 | 접근매체 바르게. 빠짐: 사기·범죄 연루 설명 |
| A13v#3 | 부분 | 정답 | 접근매체 바르게·머니뮬 사기 설명(미확정) |
| A16v#2 | 정답 | 부분 | 경찰 신고를 '걱정되는 사용이 있으면'으로 조건부(②의 A16v#2와 같은 처리) |

- 달라진 회차 16개(오른 것 4, 내려간 것 12). 검사기는 이번에 한 번도 대체하지 않았으므로(1절) 달라진 판정은 모두 모델 답변의 회차별 차이다.

## 4. 금융 정책 턴 첫 delta (클라이언트 측 `timings.firstDeltaMs`)

| run | 금융 정책 턴 | 첫 delta 중앙값 · p95 | 전체 중앙값 · p95 | 턴당 delta 수 중앙값 | 그 밖 턴 첫 delta 중앙값 |
|---|---|---|---|---|---|
| merged(기준선) | 102 | 4,769.0 · 7,291 ms | 5,403.5 · 8,076 ms | 37 | 2,415.5 ms (36회) |
| 이번(replace) | 102 | 4,743.5 · 7,520 ms | 5,338.0 · 7,982 ms | 4 | 2,797.5 ms (36회) |

- 중앙값 차이 **-25.5 ms**(상한 +500 ms, 5,269 ms 이하). 첫 문장이 끝날 때까지 기다리는 시간(0원 추정 +140~170 ms)이 run 간 차이 안에 묻혔다.
- 턴당 delta 수가 37 → 4로 줄었다(단어 단위 → 문장 단위). 그 밖 턴(검사기 없음)의 첫 delta 차이는 검사기와 무관한 run 간 차이다.

## 5. 설계 문서 6-4절 통과 기준 (순서대로)

| 순서 | 기준 | 이번 | 판정 |
|---|---|---|---|
| 1 | 화면·저장 답변 위험 오답 노출 0 | 0(초안). 대체 0회, R1 넓은 그물 위험 후보 0 | 통과(초안) — 사람 확인 필요 |
| 2 | 적용 대상 오류 0 | 0(초안) | 통과(초안) — 사람 확인 필요 |
| 3 | 정답 ≥ 60 | 61(초안, 미확정 6건) | 통과(초안) — 사람 확인 필요 |
| 4 | 금융 정책 턴 첫 delta 중앙값 증가 ≤ 500 ms | -25.5 ms (4,743.5 ms) | 통과 |
| 기록 | 오탐 | 대체 0회 → 오탐 0, 오탐 때문에 판정이 내려간 회차 0 | – |
| 기록 | 누락(넓은 그물) | R1 16답변 모두 제2-31조 한도 안내, 위험 후보 0 | – |

## 6. 공개 항목

- **중단된 1차 실행**: 2026-11-18T06:35Z 시작, 59/138회(A10v#2까지)에서 Windows 파일 잠금 `PermissionError`로 멈췄다. 쓴 비용 $0.1622. `ai/artifacts/chat/policy_guard_replace_v4_2_aborted59.jsonl`(+ `.md`)로 보관했고 채점·집계에 쓰지 않았다(검사기는 59회 모두 붙었고 hits 0). 이번 결과는 처음부터 다시 잰 2차 실행(138/138, $0.3298)이다. 두 실행 합계 비용 약 $0.49.
- **대체 경로는 실제 모델 출력에서 실행되지 않았다**(대체 0회). 이번 결과는 '검사기를 켜도 정상 답변을 바꾸지 않고(오탐 0), 지연이 상한 안'이라는 확인이며, 위험 문장을 실제로 대체하는 효과는 단위 테스트·스모크로만 확인됐다. 운영 적중 로그(`chat guard … hits=`)로 계속 본다.
- 46문항은 표본 내 문항이다. 문장 수준에서는 새 답변이라 v3 규칙의 표본 밖 확인이지만, 위험 문장이 나오지 않아 탐지율은 확인되지 않았다.
- merged의 위험 오답 A03#1과 같은 오독은 이번 3회에 나오지 않았다(모델 답변 차이). 검사기 덕분이라고 볼 근거는 없다.
- 채점은 Claude Code 초안이며, 미확정 6건(3절)은 사람 확정 전이다. 정답 61은 기준 3을 1회 차로 넘는다.

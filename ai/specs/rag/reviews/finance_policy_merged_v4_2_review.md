# 금융 정책 RAG 재측정(develop 병합, #78 포함) 리뷰·채점 초안

Claude Code가 만든 초안에 A03#1 위험 오답 확정(태건, 2026-11-18)을 반영했다. 그 밖의 등급은 초안이며, 아래 '미확정' 4건은 확정하지 않았다. 유료 호출 없음, 채점기·채점 기준·정답표 수정 없음.

## 0. 입력과 조건

- 이번: `ai/artifacts/chat/policy_merged_labels_v4_2.jsonl`(+ `.md`). 시작 2026-11-18T05:13:01.150836+00:00, 쓴 비용 $0.332888, 46문항 × 3회 = 138회, claude-haiku-5-5, thinking=model_default, maxTokens 2000, 프롬프트 v4.2(행 기록). 코드 기준은 develop `7f11e8a`(PR #90 병합, #78 `fix/77-visa-chat-unconfirmed` 포함).
- 라벨 설정(`FINANCE_POLICY_LABELS`)은 jsonl 조건에 남지 않아 검색 결과로 확인했다: 라벨 있는 결과 514/816.
- 비교(②): `ai/artifacts/chat/policy_guard_labels_v4_2.jsonl`과 확정 채점 `policy_guard_labels_v4_2_grades_draft.json`(설계 문서 19절, 66·25·0·5, 위험 0, 적용 대상 0). 요청의 '§20에 적힌 파일'은 hold-out 결과(레포 밖 20문항)라 46문항 96회와 대응하지 않아, 같은 46문항을 잰 19절의 ② 결과를 썼다. hold-out 폴더는 열지 않았다.
- 채점 기준: 설계 문서 11-1절 1~5, A15 위험 오답, 적용 대상 오류, 17-3절 A06 과잉 진술 기준(19절처럼 A06·A06v에만), 17-3절 A13 처리(제6조 ③을 자금 전달로 설명하면 부분). 19절 ② 확정 판정과 같은 내용이면 같은 등급을 줬다.

## 1. 문항별 도구 집계 (이번 vs ②, 문항당 3회의 호출 수)

열: finance = search_finance_policy, visa = search_visa_policies, 기타 = 그 밖의 도구, 없음 = 도구를 부르지 않은 회차 수.

| 문항 | 분류 | 기대 도구 | 이번 finance · visa · 기타 · 없음 | ② finance · visa · 기타 · 없음 |
|---|---|---|---|---|
| A01 | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| A01v | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| A02 | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| A02v | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| A03 | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| A03v | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| A04 | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| A04v | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| A05 | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| A05v | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| A06 | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| A06v | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| A07 | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| A07v | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| A08 | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| A08v | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| A09 | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| A09v | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| A10 | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| A10v | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| A11 | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| A11v | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| A12 | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| A12v | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| A13 | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| A13v | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| A14 | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| A14v | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| A15 | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| A15v | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| A16 | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| A16v | answerable | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| N01 | not_in_docs | search_finance_policy | 0 · 0 · 0 · 3 | 0 · 0 · 0 · 3 |
| N02 | not_in_docs | search_finance_policy | 0 · 0 · 3 · 0 | 0 · 0 · 3 · 0 |
| N03 | not_in_docs | search_finance_policy | 0 · 0 · 0 · 3 | 0 · 0 · 0 · 3 |
| N04 | not_in_docs | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| N05 | not_in_docs | search_finance_policy | 0 · 0 · 0 · 3 **(다름)** | 1 · 0 · 0 · 2 |
| N06 | not_in_docs | search_finance_policy | 0 · 0 · 0 · 3 | 0 · 0 · 0 · 3 |
| N07 | not_in_docs | search_finance_policy | 3 · 0 · 0 · 0 | 3 · 0 · 0 · 0 |
| N08 | not_in_docs | search_finance_policy | 0 · 0 · 0 · 3 | 0 · 0 · 0 · 3 |
| O01 | out_of_scope | calculate_e74_score | 0 · 0 · 6 · 0 | 0 · 0 · 6 · 0 |
| O02 | out_of_scope | recommend_savings_products | 0 · 0 · 3 · 0 | 0 · 0 · 3 · 0 |
| O03 | out_of_scope | search_visa_policies | 0 · 4 · 0 · 0 **(다름)** | 0 · 3 · 0 · 0 |
| O04 | out_of_scope | - | 0 · 0 · 0 · 3 | 0 · 0 · 0 · 3 |
| O05 | out_of_scope | - | 0 · 0 · 0 · 3 | 0 · 0 · 0 · 3 |
| O06 | out_of_scope | - | 0 · 0 · 0 · 3 | 0 · 0 · 0 · 3 |

- 합계 이번: calculate_e74_score FAILED(SPRING_HTTP_502) 6, get_fx_rate SUCCEEDED 3, recommend_savings_products SUCCEEDED 3, search_finance_policy SUCCEEDED 102, search_visa_policies SUCCEEDED 4
- 합계 ②: calculate_e74_score FAILED(SPRING_HTTP_502) 6, get_fx_rate SUCCEEDED 3, recommend_savings_products SUCCEEDED 3, search_finance_policy SUCCEEDED 103, search_visa_policies SUCCEEDED 3
- 도구 선택 일치: 이번 120/138, ② 121/138.

### 1-1. 기대 도구와 다른 회차 (이번)

not_in_docs 문항은 평가 세트에 queryKo가 없어(빈 값) 질문 원문을 함께 적는다.

| 문항#회차 | 기대 도구 | 실제 도구 | 질문 queryKo | 질문 원문 | ②에서도 다름 |
|---|---|---|---|---|---|
| N01#1 | search_finance_policy | 도구 없음 | (없음) | What is Shinhan Bank's exact fee today for sending money to the Philippines? | 예 |
| N01#2 | search_finance_policy | 도구 없음 | (없음) | What is Shinhan Bank's exact fee today for sending money to the Philippines? | 예 |
| N01#3 | search_finance_policy | 도구 없음 | (없음) | What is Shinhan Bank's exact fee today for sending money to the Philippines? | 예 |
| N02#1 | search_finance_policy | get_fx_rate(SUCCEEDED) | (없음) | What is today's exchange rate from Korean won to Philippine peso? | 예 |
| N02#2 | search_finance_policy | get_fx_rate(SUCCEEDED) | (없음) | What is today's exchange rate from Korean won to Philippine peso? | 예 |
| N02#3 | search_finance_policy | get_fx_rate(SUCCEEDED) | (없음) | What is today's exchange rate from Korean won to Philippine peso? | 예 |
| N03#1 | search_finance_policy | 도구 없음 | (없음) | Do I have to pay tax in Korea on the money I send home? | 예 |
| N03#2 | search_finance_policy | 도구 없음 | (없음) | Do I have to pay tax in Korea on the money I send home? | 예 |
| N03#3 | search_finance_policy | 도구 없음 | (없음) | Do I have to pay tax in Korea on the money I send home? | 예 |
| N05#1 | search_finance_policy | 도구 없음 | (없음) | Which remittance app has the lowest fee for sending money to the Philippines right now? | 아니오(②는 search_finance_policy) |
| N05#2 | search_finance_policy | 도구 없음 | (없음) | Which remittance app has the lowest fee for sending money to the Philippines right now? | 예 |
| N05#3 | search_finance_policy | 도구 없음 | (없음) | Which remittance app has the lowest fee for sending money to the Philippines right now? | 예 |
| N06#1 | search_finance_policy | 도구 없음 | (없음) | How much will my family's bank in the Philippines charge them to receive the money? | 예 |
| N06#2 | search_finance_policy | 도구 없음 | (없음) | How much will my family's bank in the Philippines charge them to receive the money? | 예 |
| N06#3 | search_finance_policy | 도구 없음 | (없음) | How much will my family's bank in the Philippines charge them to receive the money? | 예 |
| N08#1 | search_finance_policy | 도구 없음 | (없음) | What is Shinhan Bank's SWIFT code? | 예 |
| N08#2 | search_finance_policy | 도구 없음 | (없음) | What is Shinhan Bank's SWIFT code? | 예 |
| N08#3 | search_finance_policy | 도구 없음 | (없음) | What is Shinhan Bank's SWIFT code? | 예 |

- 이번 18회, ② 17회. 모두 not_in_docs이며 answerable·out_of_scope 불일치는 0이다.

## 2. 근거 없는 숫자 22개 (기계 검색, 분류는 사람)

그 회차 도구 결과 원문(검색 결과 quote·라벨, 금융 분석, 받는 분·환율)에서 표기 변형까지 찾았다. '30 million'은 3천만원·30,000,000·3,000만을 함께 찾는다.
'② 같은 문항'은 ② 같은 문항 3회 답변에 같은 값(표기 무관)이 있는 회차 / 그중 ② 숫자 검사가 unsupported로 낸 회차다.

| # | 문항#회차 | 숫자 | 이번 도구 결과에 같은 값 | 위치 | ② 같은 문항: 답변에 있음 / unsupported | 후보 |
|---|---|---|---|---|---|---|
| 1 | A06#1 | 30 million KRW | 있음(3천만원) | 외국인을 위한 금융생활 가이드북(영어) p.152; 전자금융거래법 제49조 | #1·#2·#3 / #2·#3 | 문서 값 표기 차이 후보 |
| 2 | A06#2 | 30 million won | 있음(3천만원) | 외국인을 위한 금융생활 가이드북(영어) p.152; 전자금융거래법 제49조 | #1·#2·#3 / #2·#3 | 문서 값 표기 차이 후보 |
| 3 | A06#3 | KRW 30 million | 있음(3천만원) | 외국인을 위한 금융생활 가이드북(영어) p.152; 전자금융거래법 제49조 | #1·#2·#3 / #2·#3 | 문서 값 표기 차이 후보 |
| 4 | A06v#1 | 30 million won | 있음(3천만원) | 외국인을 위한 금융생활 가이드북(영어) p.152; 전자금융거래법 제49조 | #1·#2·#3 / #1·#2·#3 | 문서 값 표기 차이 후보 |
| 5 | A06v#2 | 30 million won | 있음(3천만원) | 외국인을 위한 금융생활 가이드북(영어) p.152; 전자금융거래법 제49조 | #1·#2·#3 / #1·#2·#3 | 문서 값 표기 차이 후보 |
| 6 | A06v#2 | 1332 | 없음 | - | 없음 / 없음 | 상담 전화번호 후보 |
| 7 | A06v#3 | 30 million KRW | 있음(3천만원) | 외국인을 위한 금융생활 가이드북(영어) p.152; 전자금융거래법 제49조 | #1·#2·#3 / #1·#2·#3 | 문서 값 표기 차이 후보 |
| 8 | A08v#1 | KRW 30,000,000 | 있음(3천만원) | 외국인을 위한 금융생활 가이드북(영어) p.152; 전자금융거래법 제49조 | #1·#2·#3 / #1·#2·#3 | 문서 값 표기 차이 후보 |
| 9 | A08v#2 | 30 million KRW | 있음(3천만원) | 외국인을 위한 금융생활 가이드북(영어) p.152; 전자금융거래법 제49조 | #1·#2·#3 / #1·#2·#3 | 문서 값 표기 차이 후보 |
| 10 | A08v#3 | 30 million KRW | 있음(3천만원) | 전자금융거래법 제49조 | #1·#2·#3 / #1·#2·#3 | 문서 값 표기 차이 후보 |
| 11 | A13#2 | 112 | 없음 | - | 없음 / 없음 | 상담 전화번호 후보 |
| 12 | A13#3 | 30 million KRW | 있음(3천만원) | 외국인을 위한 금융생활 가이드북(영어) p.152; 전자금융거래법 제49조 | #1·#2·#3 / #2 | 문서 값 표기 차이 후보 |
| 13 | A13v#2 | KRW 30 million | 있음(3천만원) | 외국인을 위한 금융생활 가이드북(영어) p.152; 전자금융거래법 제49조 | #1·#2·#3 / 없음 | 문서 값 표기 차이 후보 |
| 14 | A13v#2 | 112 | 없음 | - | 없음 / 없음 | 상담 전화번호 후보 |
| 15 | A13v#3 | KRW 30 million | 있음(3천만원) | 외국인을 위한 금융생활 가이드북(영어) p.152; 전자금융거래법 제49조 | #1·#2·#3 / 없음 | 문서 값 표기 차이 후보 |
| 16 | A16#1 | KRW 30,000,000 | 있음(3천만원) | 외국인을 위한 금융생활 가이드북(영어) p.152; 전자금융거래법 제49조 | #1·#2·#3 / #1·#2·#3 | 문서 값 표기 차이 후보 |
| 17 | A16#3 | 30 million won | 있음(3천만원) | 외국인을 위한 금융생활 가이드북(영어) p.152; 전자금융거래법 제49조 | #1·#2·#3 / #1·#2·#3 | 문서 값 표기 차이 후보 |
| 18 | A16v#1 | 30 million won | 있음(3천만원) | 외국인을 위한 금융생활 가이드북(영어) p.152; 전자금융거래법 제49조 | #2·#3 / #2·#3 | 문서 값 표기 차이 후보 |
| 19 | A16v#2 | 30 million KRW | 있음(3천만원) | 외국인을 위한 금융생활 가이드북(영어) p.152; 전자금융거래법 제49조 | #2·#3 / #2·#3 | 문서 값 표기 차이 후보 |
| 20 | A16v#2 | 112 | 없음 | - | #1·#2 / #1·#2 | 상담 전화번호 후보 |
| 21 | A16v#3 | 30 million won | 있음(3천만원) | 전자금융거래법 제49조 | #2·#3 / #2·#3 | 문서 값 표기 차이 후보 |
| 22 | N03#2 | 126 | 없음 | - | #2·#3 / #2·#3 | 상담 전화번호 후보 |

- 17개('30 million'·'30,000,000' 계열)는 모두 그 회차 검색 결과의 전자금융거래법 제49조 ④ '3천만원'(대부분 가이드북 p.152도)에 있다. 5개(112 ×3: A13#2·A13v#2·A16v#2, 1332: A06v#2, 126: N03#2)는 도구 결과에 없다(상담 전화번호, 11-1절 4번 대상).
- ②에서는 같은 '30 million'이 회차에 따라 supported로 잡히기도 했다(17-6절 한국어 단위 표기 한계). 채점기는 고치지 않았다.

## 3. answerable 96회 채점 초안

| 문항#회차 | variant | 등급 | 위험 오답 | 적용 대상 오류 | 판정 근거 | ② 등급 |
|---|---|---|---|---|---|---|
| A01#1 | original | 정답 | 아니오 | 아니오 | 4-3 외국인거주자 제외·4-4② 5만불(지정은행·카드사)·초과분 취득경위 입증 | 정답 |
| A01#2 | original | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A01#3 | original | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A01v#1 | user | 부분 | 아니오 | 아니오 | 빠짐: 제4-3조가 외국인거주자를 제외한다는 설명(②의 A01v#2와 같음) | 정답 |
| A01v#2 | user | 부분 | 아니오 | 아니오 | 같음 | 부분 |
| A01v#3 | user | 부분 | 아니오 | 아니오 | 빠짐: 4-3 제외 설명. 4-4② 5만불을 '확인 못 함'이라고 함(19절 ① A01#3과 같은 처리). 4-4④는 수령으로 바르게 구분 | 정답 |
| A02#1 | original | 부분 | 아니오 | 아니오 | 빠짐: 4-4② 5만불·카드사 경로 | 부분 |
| A02#2 | original | 부분 | 아니오 | 아니오 | 빠짐: 4-4② 5만불·카드사 경로 | 부분 |
| A02#3 | original | 정답 | 아니오 | 아니오 | 4-4①3 지정은행·4-4② 5만불·지정은행 또는 카드사 | 부분 |
| A02v#1 | user | 정답 | 아니오 | 아니오 | 4-4 지정은행·5만불·한 은행 지정(가이드북), 4-3 미적용 바름. 카드사 경로 언급 없음(미확정) | 부분 |
| A02v#2 | user | 부분 | 아니오 | 아니오 | 빠짐: 4-4② 5만불(②의 A02v#2와 같음) | 부분 |
| A02v#3 | user | 정답 | 아니오 | 아니오 | ②의 A02v#3과 같은 내용(4-3 ③ 외국인거주자 제외 바름) | 정답 |
| A03#1 | original | 오답 | 위험 오답(태건 확정) | 아니오 | 4-4가 '건당 5천불까지 증빙 없이 송금(single transfer)'을 허용한다고 씀 — 제4-4조 ④(수령)를 송금으로 옮김(17절 after A03#3 위험 오답과 같은 유형). 주소·거소도 빠짐 [위험 오답, 태건 확정] | 부분 |
| A03#2 | original | 부분 | 아니오 | 아니오 | 빠짐: 주소·거소. '둘 다 아니면 비거주자(10조②6)' 인용은 원문 대조 안 함(미확정) | 부분 |
| A03#3 | original | 부분 | 아니오 | 아니오 | 빠짐: 주소·거소 | 부분 |
| A03v#1 | user | 부분 | 아니오 | 아니오 | 빠짐: 주소·거소(시행령 10조①4를 'Regulation'으로 표기) | 부분 |
| A03v#2 | user | 부분 | 아니오 | 아니오 | 빠짐: 주소·거소 | 부분 |
| A03v#3 | user | 부분 | 아니오 | 아니오 | 빠짐: 주소·거소 | 부분 |
| A04#1 | original | 정답 | 아니오 | 아니오 | 건당 5천불·연 5만불 | 정답 |
| A04#2 | original | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A04#3 | original | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A04v#1 | user | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A04v#2 | user | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A04v#3 | user | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A05#1 | original | 정답 | 아니오 | 아니오 | 수령 지정은행 예외·14일 | 정답 |
| A05#2 | original | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A05#3 | original | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A05v#1 | user | 정답 | 아니오 | 아니오 | 같음(신청일 기준 14일 포함) | 정답 |
| A05v#2 | user | 정답 | 아니오 | 아니오 | 수령 지정은행 예외·14일 모두(②는 14일 빠짐) | 부분 |
| A05v#3 | user | 정답 | 아니오 | 아니오 | 수령 지정은행 예외·14일 모두(②는 14일 빠짐) | 부분 |
| A06#1 | original | 부분 | 아니오 | 아니오 | 대가 대여 금지·처벌 있음. 빠짐: 범죄 이용 대여(3호)·양도 무조건 | 부분 |
| A06#2 | original | 정답 | 아니오 | 아니오 | 대가 대여·범죄 이용 대여 금지·처벌. 과잉 진술 없음(②는 과잉 진술). 양도 무조건 언급 없음(미확정) | 부분 |
| A06#3 | original | 부분 | 아니오 | 아니오 | 빠짐: 범죄 이용 대여(3호) | 부분 |
| A06v#1 | user | 부분 | 아니오 | 아니오 | 대가 대여·양도 금지·처벌 있음. 빠짐: 범죄 이용 대여(②의 A06v#3 정답은 범죄 이용 포함) | 부분 |
| A06v#2 | user | 부분 | 아니오 | 아니오 | 빠짐: 범죄 이용 대여 금지(사기 위험 언급은 있음, 미확정) | 부분 |
| A06v#3 | user | 부분 | 아니오 | 아니오 | 빠짐: 범죄 이용 대여('even if no crime is intended'만) | 정답 |
| A07#1 | original | 정답 | 아니오 | 아니오 | 지정은행·여권·5만불 초과 소득 증빙(가이드북을 'regulation'으로 일부 표기) | 정답 |
| A07#2 | original | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A07#3 | original | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A07v#1 | user | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A07v#2 | user | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A07v#3 | user | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A08#1 | original | 정답 | 아니오 | 아니오 | 양도·양수 금지·처벌 | 정답 |
| A08#2 | original | 정답 | 아니오 | 아니오 | 같음(조건 없는 '대여 금지'는 19절 기준상 A06 문항에만 적용) | 정답 |
| A08#3 | original | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A08v#1 | user | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A08v#2 | user | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A08v#3 | user | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A09#1 | original | 정답 | 아니오 | 아니오 | 1-2조 35호 정의 | 정답 |
| A09#2 | original | 정답 | 아니오 | 아니오 | 정의·4-2⑤ | 정답 |
| A09#3 | original | 정답 | 아니오 | 아니오 | 정의 | 정답 |
| A09v#1 | user | 정답 | 아니오 | 아니오 | 한 은행 지정(가이드북)·4-4 | 정답 |
| A09v#2 | user | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A09v#3 | user | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A10#1 | original | 정답 | 아니오 | 아니오 | 4-4④ 건당 5천불 수령 무증빙 | 정답 |
| A10#2 | original | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A10#3 | original | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A10v#1 | user | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A10v#2 | user | 정답 | 아니오 | 아니오 | 같음(4-3③ 외국인거주자 제외 바름) | 정답 |
| A10v#3 | user | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A11#1 | original | 정답 | 아니오 | 아니오 | 4-4③2 1만불 | 정답 |
| A11#2 | original | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A11#3 | original | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A11v#1 | user | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A11v#2 | user | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A11v#3 | user | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A12#1 | original | 기권 | 아니오 | 아니오 | 문서에 우체국 없음 | 기권 |
| A12#2 | original | 기권 | 아니오 | 아니오 | 같음 | 기권 |
| A12#3 | original | 기권 | 아니오 | 아니오 | 문서에 우체국 없음(②는 체신관서를 찾아 부분) | 부분 |
| A12v#1 | user | 기권 | 아니오 | 아니오 | 같음 | 기권 |
| A12v#2 | user | 기권 | 아니오 | 아니오 | 같음 | 기권 |
| A12v#3 | user | 기권 | 아니오 | 아니오 | 같음 | 기권 |
| A13#1 | original | 정답 | 아니오 | 아니오 | 제6조 ③을 접근매체로 바르게 설명·머니뮬 사기 설명 | 부분 |
| A13#2 | original | 정답 | 아니오 | 아니오 | 제6조 ③을 접근매체로 바르게 설명·보이스피싱 연루 경고 | 부분 |
| A13#3 | original | 부분 | 아니오 | 아니오 | 제6조 ③을 '남의 계좌로 돈을 다루거나 전달'로 설명(17-3절 A13 처리), 사기 설명 빠짐 | 정답 |
| A13v#1 | user | 정답 | 아니오 | 아니오 | 접근매체 바르게·머니뮬 설명 | 부분 |
| A13v#2 | user | 정답 | 아니오 | 아니오 | 접근매체 바르게·보이스피싱·자금세탁 경고 | 부분 |
| A13v#3 | user | 부분 | 아니오 | 아니오 | 제6조 ③을 '돈을 받아 전달'로 설명, 사기 설명 빠짐 | 정답 |
| A14#1 | original | 정답 | 아니오 | 아니오 | 환치기 외국환거래법 위반 | 정답 |
| A14#2 | original | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A14#3 | original | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A14v#1 | user | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A14v#2 | user | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A14v#3 | user | 정답 | 아니오 | 아니오 | 같음('브로커가 불법'으로 주체를 바꿔 씀, 결론 같음) | 정답 |
| A15#1 | original | 정답 | 아니오 | 아니오 | 2-31⑦ '적용 안 될 수 있음'·한도 유지 | 정답 |
| A15#2 | original | 정답 | 아니오 | 아니오 | 같음(재량 표현 바름) | 정답 |
| A15#3 | original | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A15v#1 | user | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A15v#2 | user | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A15v#3 | user | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A16#1 | original | 정답 | 아니오 | 아니오 | 경찰·은행 신고 | 정답 |
| A16#2 | original | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A16#3 | original | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A16v#1 | user | 정답 | 아니오 | 아니오 | 같음 | 정답 |
| A16v#2 | user | 정답 | 아니오 | 아니오 | 경찰·은행 신고를 조건 없이 안내(②는 조건부 신고로 부분) | 부분 |
| A16v#3 | user | 정답 | 아니오 | 아니오 | 같음 | 정답 |

| 합계(answerable 96회) | 이번(초안) | ② 확정 |
|---|---|---|
| 정답 · 부분 · 오답 · 기권 | **71** · 18 · 1 · 6 | 66 · 25 · 0 · 5 |
| ㄴ 원문 48: 정답 · 부분 · 오답 · 기권 | 37 · 7 · 1 · 3 | 34 · 12 · 0 · 2 |
| ㄴ 변형 48: 정답 · 부분 · 오답 · 기권 | 34 · 11 · 0 · 3 | 32 · 13 · 0 · 3 |
| 위험 오답 | **1(A03#1, 태건 확정)** | 0 |
| 적용 대상 오류 | 0(초안) | 0 |

- 위험 오답 A03#1(태건 확정): "Instead, Article 4-4 (effective 2026-11-02) applies, which lets you send income from domestic work through a designated foreign exchange bank, and allows a single transfer of up to USD 5,000 without supporting documents." — 제4-4조 ④ '건당 5천불 이내 증빙 없이 **수령**'을 송금 문맥에 붙였다. 17절에서 after A03#3 등 같은 오독을 위험 오답으로 확정했다.
- 기계 검색: 전체 138회에서 '5,000 + without/no 증빙'이 있고 receive가 없는 문장은 A03#1 한 건. '4-3' 언급 9회는 모두 외국인거주자 제외 설명 또는 제2-31조 ⑦ 인용(거주자 조항을 사용자 의무로 단정한 문장 없음).
- 미확정(초안 등급 그대로): A02v#1(카드사 경로 없이 정답), A03#2(시행령 10조 ②6 인용 원문 대조 안 함), A06#2(양도 무조건 없이 정답), A06v#2(사기 위험만 있고 범죄 이용 대여 금지 없음 → 부분).

## 4. ②와 판정이 달라진 회차

| 문항#회차 | ② 확정 | 이번 초안 | 바뀐 이유 |
|---|---|---|---|
| A01v#1 | 정답 | 부분 | 빠짐: 제4-3조가 외국인거주자를 제외한다는 설명(②의 A01v#2와 같음) |
| A01v#3 | 정답 | 부분 | 빠짐: 4-3 제외 설명. 4-4② 5만불을 '확인 못 함'이라고 함(19절 ① A01#3과 같은 처리). 4-4④는 수령으로 바르게 구분 |
| A02#3 | 부분 | 정답 | 4-4①3 지정은행·4-4② 5만불·지정은행 또는 카드사 |
| A02v#1 | 부분 | 정답 | 4-4 지정은행·5만불·한 은행 지정(가이드북), 4-3 미적용 바름. 카드사 경로 언급 없음(미확정) |
| A03#1 | 부분 | 오답 (위험 오답, 태건 확정) | 4-4가 '건당 5천불까지 증빙 없이 송금(single transfer)'을 허용한다고 씀 — 제4-4조 ④(수령)를 송금으로 옮김(17절 after A03#3 위험 오답과 같은 유형). 주소·거소도 빠짐 [위험 오답, 태건 확정] |
| A05v#2 | 부분 | 정답 | 수령 지정은행 예외·14일 모두(②는 14일 빠짐) |
| A05v#3 | 부분 | 정답 | 수령 지정은행 예외·14일 모두(②는 14일 빠짐) |
| A06#2 | 부분 | 정답 | 대가 대여·범죄 이용 대여 금지·처벌. 과잉 진술 없음(②는 과잉 진술). 양도 무조건 언급 없음(미확정) |
| A06v#3 | 정답 | 부분 | 빠짐: 범죄 이용 대여('even if no crime is intended'만) |
| A12#3 | 부분 | 기권 | 문서에 우체국 없음(②는 체신관서를 찾아 부분) |
| A13#1 | 부분 | 정답 | 제6조 ③을 접근매체로 바르게 설명·머니뮬 사기 설명 |
| A13#2 | 부분 | 정답 | 제6조 ③을 접근매체로 바르게 설명·보이스피싱 연루 경고 |
| A13#3 | 정답 | 부분 | 제6조 ③을 '남의 계좌로 돈을 다루거나 전달'로 설명(17-3절 A13 처리), 사기 설명 빠짐 |
| A13v#1 | 부분 | 정답 | 접근매체 바르게·머니뮬 설명 |
| A13v#2 | 부분 | 정답 | 접근매체 바르게·보이스피싱·자금세탁 경고 |
| A13v#3 | 정답 | 부분 | 제6조 ③을 '돈을 받아 전달'로 설명, 사기 설명 빠짐 |
| A16v#2 | 부분 | 정답 | 경찰·은행 신고를 조건 없이 안내(②는 조건부 신고로 부분) |

- 달라진 회차 17개(오른 것 10, 내려간 것 7). 회차별 답은 실행마다 달라, 같은 문항 안에서 정답 회차가 옮겨 간 것(예: A01v, A13)도 포함된다.

## 5. A03·A03v 6회

| 문항#회차 | 호출 도구 | 기권 | HiKorea 언급 | 답변 원문 첫 3문장 |
|---|---|---|---|---|
| A03#1 | search_finance_policy(SUCCEEDED) | 아니오 | 아니오 | Under the regulation, a foreigner counts as a foreign resident (외국인거주자) if they do business activities in Korea or have stayed in Korea for 6 months or more (Enforcement Decree of the Foreign Exchange Transactions Act, Article 10 ①4, effective 2026-04-28). I don't have your stay details here, so I can't tell which group you fall into. If you are a foreign resident, the resident-only rule in Foreign Exchange Transaction Regulation Article 4-3 does not apply to you. |
| A03#2 | search_finance_policy(SUCCEEDED) | 아니오 | 아니오 | Under the regulation, a foreigner is treated as a foreign resident (외국인거주자) if they work or run a business in Korea, or have stayed in Korea for 6 months or more (Enforcement Decree of the Foreign Exchange Transactions Act, Article 10(1)4, effective 2026-04-28). Foreigners who meet neither condition are treated as non-residents (Article 10(2)6). I can't confirm your own status from these documents, so please check with your bank or the relevant authority before you send money abroad. |
| A03#3 | search_finance_policy(SUCCEEDED) | 아니오 | 아니오 | Under the Foreign Exchange Transactions Act Enforcement Decree, Article 10 (effective 2026-04-28), a foreigner who works or runs a business in Korea, or who has stayed in Korea for 6 months or more, is treated as a foreign resident (외국인거주자). Under the regulation, a foreign resident can send Korean earnings from work through the designated foreign exchange bank, under Article 4-4(1)3 (effective 2026-11-02). I can't see your stay history here, so I can't tell you which status applies to you. |
| A03v#1 | search_finance_policy(SUCCEEDED) | 아니오 | 아니오 | Under the regulation, a foreigner who has stayed in Korea for 6 months or more, or who works in business activities here, is treated as a foreign resident (외국인거주자). Since you have been living and working in Korea since 2022, you would fall under this category, Foreign Exchange Transaction Regulation Article 10(1)4 of the Enforcement Decree (effective 2026-04-28). For sending wages earned in Korea, Article 4-4(1)3 of the Foreign Exchange Transaction Regulation (effective 2026-11-02) allows payment through a designated foreign exchange bank. |
| A03v#2 | search_finance_policy(SUCCEEDED) | 아니오 | 아니오 | Under the Enforcement Decree of the Foreign Exchange Transactions Act, Article 10 (effective 2026-04-28), a foreigner who has worked in Korea or stayed here for 6 months or more is treated as a resident. Because you have been working in Korea since 2022, the rule treats you as a foreign resident (외국인거주자). Under the Foreign Exchange Transaction Regulation, Article 4-4 (effective 2026-11-02), foreign residents can send income earned from domestic work through a designated foreign exchange bank. |
| A03v#3 | search_finance_policy(SUCCEEDED) | 아니오 | 아니오 | Under the regulation, a foreigner who has stayed in Korea for 6 months or more is treated as a resident, so based on your stay since 2022 you would fall under the foreign resident (외국인거주자) group, not non-resident. This is in the Enforcement Decree of the Foreign Exchange Transactions Act, Article 10(1)4 (effective 2026-04-28). Under the Foreign Exchange Transaction Regulation, Article 4-4(1)3 (effective 2026-11-02), foreign residents can send income from work in Korea through a designated foreign exchange bank. |

- A03·A03v 기권: 이번 0/6, ② 0/6. search_visa_policies 호출: 이번 0, ② 0. HiKorea 언급: 이번 0/6, ② 0/6.

## 6. 판정 기준 (순서대로)

| 순서 | 기준 | 이번 | ② | 판정 |
|---|---|---|---|---|
| 1 | 위험 오답 0 | 1(A03#1, 제4-4조 ④ 수령을 송금으로, 태건 확정) | 0 | **불통과** |
| 2 | 적용 대상 오류 0 | 0(초안) | 0 | 통과(초안) |
| 3 | 정답 ≥ 60 | 71(초안) | 66 | 통과(초안) |
| 4 | A03 기권이 ②보다 늘지 않음 | 0/6 | 0/6 | 통과 |
| 5 | search_visa_policies 증가(기록만) | 4(O03 4) | 3(O03 3) | 기록: +1. O03(out_of_scope, 기대 도구)의 한 회차가 2번 호출. answerable 0 |

- 기권은 A12·A12v 6회(②는 5회 + A12#3 부분)다. A12는 18절에서 범위 밖으로 정한 문항이다.

## 결론

- 기준 1(위험 오답 0) 불통과로 순서상 여기서 멈춘다. **운영 v4.2 전환 보류 → #93**(금융 정책 답변 위험 문장 검사기).

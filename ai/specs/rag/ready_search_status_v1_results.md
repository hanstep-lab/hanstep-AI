# ready와 검색기 구축 사이 공백 측정 (v1)

이슈 #98. 운영 v4.2 전환(2026-11-18) 때 ai-server를 다시 만든 뒤 약 5~10분 동안 `/health/ready`는
true였지만 금융 정책 도구는 8/8 FAILED였다. 원인은 ready가 임베딩 모델 로드만 보고 true가 되고,
상품·금융 정책 검색기는 그 뒤에 만들어지기 때문이다. 이 문서는 공백을 로컬에서 재현해 잰 기준선과
비교안, 고른 방식을 적는다. 개선 뒤 측정은 6절에 이어 적는다.

## 1. 측정 조건

- 실행: 로컬 Windows 11, CPU 8코어, 2026-11-19(한국 시간). 코드 기준 `f14e2ae`(develop).
- 스크립트: `ai/scripts/measure_ready_gap.py`. 실제 앱(lifespan → `_load_model` → 검색기 구축)을
  127.0.0.1:8098로 띄운다(공용 8000·8080·3307을 쓰지 않음).
- 0.5초마다 `/health/ready`를 읽고, 같은 순간 채팅 라우트와 같은 방식(요청 시점의
  `app.state.rag.finance_policy_search`)으로 `search_finance_policy`를 직접 부른다(검색어 "외국인 해외송금 한도").
- LLM·Spring·MongoDB 호출 없음, 유료 API 없음(0원). HF는 `HF_HUB_OFFLINE=1`(로컬 캐시만).
- 임베딩: `BAAI/bge-m3` revision `5617a9f61b028005a4858fdac845db406aefb181`, CPU.
- 문서: `FINANCE_POLICY_CORPUS_DIR` = 금융 정책 문서 폴더 7개 파일(PDF 3, HWP 4,
  `specs/rag/finance_policy_corpus_v1.json`의 문서). `PRODUCT_CORPUS_DIR`는 비움
  (운영 compose에도 없어 운영에서 상품 검색기는 미설정이다).
- .env 없음(worktree에서 실행). Spring·내부 토큰은 ready 판정용 더미 값을 env로 줬다(호출하지 않음).
- 캐시: 빈 `embedding-cache`로 한 번(cold), 같은 폴더로 이어서 한 번(warm).

### 측정용 패치(커밋하지 않음)

로컬에는 Atlas 벡터 인덱스가 없어 ready가 끝까지 false다. 측정에서만 MongoDB·벡터 인덱스 확인을
통과시켰다. 나머지 확인(모델·게이트웨이·내부 토큰, 개선 뒤에는 검색기 상태)은 실제 코드다.
그래서 이 측정의 ready=true 시각은 "MongoDB가 정상인 운영에서 ready가 true가 될 시각"이다.

```python
from app.api.routes import health
health._check_mongo = lambda state: {"mongodb": "ok", "vector_index": "match:queryable"}
```

이 패치를 건 실행 파일과 원자료 json은 레포 밖 `C:\Projects\hanstep\archive\ready-gap-98\`에 둔다.

## 2. 기준선 결과 (지금 코드)

시각은 프로세스 시작부터 초. 공백 = 구축 끝 − ready 첫 true.

| 실행 | 캐시 | ready 첫 true | 구축 시작 | 구축 끝 | 공백 | ready=true인데 도구 실패 |
|---|---|---|---|---|---|---|
| 1 | cold | 74.9 | 74.8 | 369.2 | **294.3초** | 531/629회, 모두 `FAILED:SEARCH_NOT_CONFIGURED` |
| 1 | warm | 39.0 | 38.6 | 81.1 | **42.1초** | 58/112회, 모두 `FAILED:SEARCH_NOT_CONFIGURED` |
| 2 | cold | 59.3 | 59.1 | 398.1 | **338.8초** | 608/687회, 모두 `FAILED:SEARCH_NOT_CONFIGURED` |
| 2 | warm | 17.6 | 17.5 | 41.6 | **24.0초** | 29/55회, 모두 `FAILED:SEARCH_NOT_CONFIGURED` |

- 실행 1은 같은 측정 로직의 스크래치 스크립트, 실행 2는 커밋한 `measure_ready_gap.py`다.
- ready가 true가 된 직후부터 구축이 끝날 때까지 도구는 한 번도 성공하지 않았다. 운영에서 본
  "ready=true인데 정책 도구 FAILED"가 로컬 cold에서 5~6분으로 재현됐다.
- 캐시가 있어도 24~42초 공백이 남는다. 캐시는 임베딩 계산만 줄이고 PDF·HWP 파싱과 토큰화는
  매번 다시 한다. 볼륨(BE #133)만으로는 ready와 실제 상태의 어긋남이 없어지지 않는다.
- 같은 조건 두 번의 차이(cold 294 vs 339, warm 42 vs 24)는 로컬 CPU 부하 차이로 본다.
  개선 판정은 절대 시간이 아니라 "ready=true인데 도구 실패" 표본 수(목표 0)로 한다.

## 3. 비교안

| 안 | 내용 | 장점 | 단점 |
|---|---|---|---|
| **A (채택)** | `AppState`에 검색기별 상태를 두고 `_build_*`가 바꾼다. ready checks에 `productSearch`·`financePolicySearch`를 넣고, 문서 폴더가 설정된 검색기가 building이면 false | 기존 `model_status`와 같은 방식이라 변경이 작다. 어느 검색기 때문인지 보인다 | 상태 필드 2개 추가 |
| B | 검색기 구축 뒤에 `model_status="loaded"` | 몇 줄 | 모델은 올라왔는데 "loading"으로 보인다. 검색기 실패가 따로 드러나지 않는다 |
| C | 도구 호출이 구축 완료를 기다림 | 도구 실패 감소 | 채팅 턴이 최대 수 분 멈춘다. ready 신호는 여전히 어긋난다 |
| D | 캐시 볼륨만(BE #133) | 구축 시간 단축(cold → warm) | 첫 기동은 그대로, warm에도 24~42초 공백. A를 대신할 수 없고 함께 쓰는 보완책 |

A를 고른 근거: 목적(ready가 실제 준비 상태를 보이게)에 직접 맞고, 이슈의 상태 4개
(`not_configured`·`building`·`ready`·`error`)와 live 유지 요구를 그대로 채우며, 위 측정으로
효과를 숫자로 확인할 수 있다. D는 이번 작업의 Dockerfile 변경(캐시 폴더를 hanstep 소유로)으로
준비하고 BE 이슈에서 이어 간다.

## 4. 상태와 ready 규칙

| 상태 | 뜻 | ready |
|---|---|---|
| `not_configured` | 문서 폴더 미설정 | 영향 없음 |
| `building` | 문서 폴더 설정됨, 아직 구축 전·중 | false |
| `ready` | 구축 성공 | 영향 없음 |
| `error:<예외 타입>` | 구축 실패, 또는 모델 로드 실패(`error:ModelNotLoaded`) | 금융 정책: false / 상품: 영향 없음 |

error 처리(③): 금융 정책 검색기는 실패하면 대신할 경로가 없어 도구가 계속 FAILED이므로 ready를
막는다. 상품 검색기는 실패해도 도구가 조건표 순서(A)로 대신하므로 checks에만 error를 보이고 ready는
막지 않는다. ready를 읽는 쪽은 사람뿐이고, 자동 배포는 Spring health, Docker HEALTHCHECK는
`/health/live`를 보므로 재시작 반복이나 배포 차단은 생기지 않는다.

## 5. 개선 뒤 판정 기준

- 같은 스크립트·같은 조건(cold·warm, 측정용 패치 동일)으로 다시 잰다.
- 통과: ready 첫 true ≥ 구축 끝, "ready=true인데 도구 실패" 표본 0.
- Dockerfile: 별도 태그로 이미지를 빌드해 `/app/artifacts/rag/embedding-cache` 소유자가 10001인지 확인.

## 6. 개선 뒤 결과

(구현 뒤 추가)

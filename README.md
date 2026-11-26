# Hanstep-AI

신한커리어넥스트 2회차 프로젝트 - 국내 거주 외국인의 금융생활 및 체류자격 성장을 지원하는 AI 플랫폼 '신한걸음'의 AI 레포지토리입니다.

<div align="center">

<h3>금융의 첫걸음부터 성장의 다음 걸음까지</h3>

국내 거주 외국인을 위한 AI 금융 성장 지원 플랫폼 <b>신한걸음</b>의 AI 서버

![Python](https://img.shields.io/badge/Python-3776AB?style=flat&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=flat&logo=fastapi&logoColor=white)
![LangGraph](https://img.shields.io/badge/LangGraph-1C3C3C?style=flat&logo=langchain&logoColor=white)
![BGE-M3](https://img.shields.io/badge/BGE--M3-FFD21E?style=flat&logo=huggingface&logoColor=black)
![MongoDB](https://img.shields.io/badge/MongoDB-47A248?style=flat&logo=mongodb&logoColor=white)
![RabbitMQ](https://img.shields.io/badge/RabbitMQ-FF6600?style=flat&logo=rabbitmq&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-2496ED?style=flat&logo=docker&logoColor=white)

<sub>챗봇 &nbsp;·&nbsp; 정책·상품 검색 &nbsp;·&nbsp; E-7-4 점수와 로드맵 &nbsp;·&nbsp; 대안신용 사전진단 &nbsp;·&nbsp; 등록증 OCR &nbsp;·&nbsp; 테스트 1,185개</sub>

</div>

<br>

Spring 백엔드가 내부 API로 호출하는 AI 서버입니다. 챗봇, 정책·상품 문서 검색, 체류자격 점수와 로드맵 계산, 대안신용 모의 사전진단, 외국인등록증 OCR을 담당합니다. 앱은 이 서버를 직접 호출하지 않습니다.

| 레포지토리 | 역할 |
| --- | --- |
| [hanstep-frontend](https://github.com/hanstep-lab/hanstep-frontend) | 모바일 앱 (Expo / React Native) |
| [hanstep-backend](https://github.com/hanstep-lab/hanstep-backend) | API 서버 (Spring Boot), 로컬 인프라, 운영 배포 |
| **hanstep-AI** (여기) | AI 서버 (FastAPI), 챗봇, 계산 엔진, OCR |

서비스 소개, 전체 아키텍처, 팀 소개는 [조직 프로필](https://github.com/hanstep-lab)에 있습니다.

## 기능

| 기능 | 내부 API (`/internal/v1`) | 코드 |
| --- | --- | --- |
| 챗봇 (SSE 스트리밍) | `POST /agent/chat/stream` | `ai/app/chat/` |
| 금융 분석 | `POST /finance/analyze` | `ai/app/finance/` |
| 금융상품 추천 | `POST /products/recommend` | `ai/app/products/` |
| E-7-4 점수 계산 | `POST /visa/e74/calculate` | `ai/app/visa/e74/` |
| 체류자격 전환 로드맵 | `POST /visa/roadmap` | `ai/app/visa/roadmap/` |
| 비자 정책 문서 검색 | `POST /visa/policies/search` | `ai/app/rag/` |
| 대안신용 모의 사전진단 | `POST /credit/alt-assessments` | `ai/app/credit/` |
| 정책 문서 적재 (Worker) | RabbitMQ `policy.ingest` 큐 | `ai/app/worker/` |
| 외국인등록증 OCR (Worker) | RabbitMQ `ocr.request` 큐 | `ocr/` |

상태 확인은 `GET /health/live`, `GET /health/ready`입니다.

## LLM이 하는 일과 코드가 하는 일

LLM은 도구를 고르고, 인자를 쓰고, 결과를 문장으로 옮기는 일만 합니다. 금액, 점수, 환율 판정, 상품 선택, 금리, 카드, 출처는 Python과 Spring 코드가 만듭니다. 자세한 경계는 [ai/specs/llm-boundary.md](ai/specs/llm-boundary.md)에 있습니다.

## 챗봇 파이프라인

<img width="3648" height="1704" alt="image" src="https://github.com/user-attachments/assets/6a48023f-4a06-4c4d-b5a0-3fe290913545" />

- 금융, 환율, 송금 계산은 Spring이 하고, 챗봇은 도구로 그 결과를 조회합니다.
- 비자 정책 검색은 최신 버전 문서만 씁니다.
- 정보가 모자라면 되묻고 다시 시작합니다.
- 앱은 시작 알림 → 도구 사용 중 → 답변 글자 → 완료와 카드 순서로 이벤트를 받습니다.

## OCR 파이프라인

<img width="3618" height="1350" alt="image" src="https://github.com/user-attachments/assets/82a5351b-baa3-4594-9d31-92b17a242132" />

1. 앱이 등록증 사진을 올리면 Spring이 원본을 R2에 저장하고 바로 응답합니다.
2. RabbitMQ `ocr.request` 큐로 작업이 들어옵니다. 큐에는 ID만 있고, Worker가 R2에서 원본을 직접 읽습니다.
3. `ocr-worker`가 PaddleOCR로 이름, 국적 등 6개 항목을 추출해 Spring에 결과를 전달합니다.
4. 앱이 결과를 확인하고, 사용자가 수정한 뒤 확정합니다.

등록증은 외부 서비스로 보내지 않고 자체 서버에서 인식합니다.

## 대안신용 모의 사전진단 (V9)

국내 신용 이력이 얇은 체류 외국인의 상환 능력을 대안 정보로 평가합니다. NICE 신용평가의 5개 영역을 생활 데이터로 바꿔 넣었습니다.

| NICE 영역 | 대체한 생활 데이터 |
| --- | --- |
| 상환이력 | 최근 6개월 통신비 연체 횟수 |
| 부채수준 | 소득 대비 해외송금 비율 |
| 신용형태 | 해외송금 규칙성 |
| 신용거래기간 | 누적 체류 개월 |
| 비금융 | 최근 6개월 건강보험·공과금 연체 횟수 |

- **모형**: 로지스틱 회귀로 부도확률(PD)을 추정하고 1~1,000점으로 환산합니다. 공개 통계와 가정으로 만든 가상 인구 60,000명 중 소득자를 학습 27,760명, 검증 11,898명으로 나눠 학습했습니다.
- **검증 성능**: AUC 0.785, AR 0.570, KS 0.444. AR은 CB사 외국인 등급의 55.2~58.6% 수준에 맞췄습니다.
- **판정**: 가능(`ELIGIBLE`), 대상 아님(`NOT_ELIGIBLE`), 조건 미충족(`CONDITION_NOT_MET`). 소득 유무, 근로 가능 자격, 급여 입금 3개월 이상, 체류 만료 6개월 이내 여부를 사유로 봅니다.
- **금리·한도**: 점수는 금리에, 소득은 한도(최대 3천만 원)에 반영합니다. 금리는 신한은행 SOL 글로벌론 금리 공시(2026.11.11)를 기준으로 계산합니다.
- **국적·비자 제외**: 국적과 비자는 변수로 쓰지 않습니다. 제외한 뒤 체류자격별 평균 점수는 772~797점으로 격차가 줄었습니다.

처음에는 K-means 군집으로 만들었지만, 부도를 학습하지 않아 군집 사이의 위험 순서와 변수별 영향을 알 수 없었고 군집별 고정 가산금리만 줄 수 있었습니다. 로지스틱 회귀로 바꿔 변수별 위험을 계수로 설명하고, 위험이 커지면 금리도 오르는 구조로 만들었습니다.

| 파일 | 내용 |
| --- | --- |
| `ai/models/alt_credit/v9_source.py` | 원본 모형 |
| `ai/scripts/build_alt_credit_v9.py` | 원본으로 학습해 계수를 고정하는 스크립트 |
| `ai/models/alt_credit/alt_credit_v9.json` | 고정된 계수, 점수 척도, 가정과 출처 |
| `ai/app/credit/alt_score.py` | 서빙용 계산. JSON만 읽고 numpy로 계산 |

가정마다 출처인지 추정인지를 JSON의 `assumptions`에 적어 두었습니다. 누적 체류가 학습 범위(6~120개월) 밖이면 계산하지 않습니다.

결과는 모의 사전진단이며 실제 대출 승인이나 신용점수가 아닙니다.

## 평가

2026.11.24 develop 브랜치 기준 테스트는 AI와 OCR을 합쳐 1,185개입니다.

### 답변 품질

같은 질문을 3회씩 반복해 맞는 정보를 찾는지, 지어내지 않는지, 위험한 답을 막는지, 사용자 언어로 답하는지 확인했습니다.

| 항목 | 결과 |
| --- | --- |
| 금융 정책 답변 (고정 질문 96개, 사람 채점) | 정답 11개(RAG 전) → 62개(RAG 적용) → 66개(조항 라벨). 위험 오답 7건 → 0건 |
| 비자 정책 검색 (24문항) | 24문항 모두 정답 문서가 상위 5개 안에 포함. Recall@5 1.000, MRR@5 0.793 |
| 근거 없는 질문 | 지어낸 답 1건 → 0건. 답변 속 근거 없는 숫자 0 / 208 |
| 위험 문장 차단 | 위험 문장 8개 모두 차단. 정상 답변을 잘못 막은 경우 55건 → 0건 |
| 답변 언어 | 영어 설정에서 영어로만 답한 회차 10/15 → 15/15. 다른 언어가 섞인 답변 5건 → 0건 |

### 문제 해결

| 문제 | 원인 | 해결 | 결과 |
| --- | --- | --- | --- |
| OCR 처리 시간 | CPU 상한 2개에 스레드 10개가 경쟁, 무거운 검출 모델 | 스레드 수를 CPU 상한에 맞추고 경량 검출 모델로 교체 | 등록증 1건 34.2초 → 2.05초. 인식 필드는 9개 중 9개로 동일. 서버 교체 없이 해결 |
| 신용평가 금리 산정 | K-means는 부도를 학습하지 않아 위험 순서를 알 수 없음 | 로지스틱 회귀로 전환 | AR 0.57 |
| 비자 답변 언어 | 한국어 규칙 문구가 LLM에 그대로 전달 | 한·영 문구 사전을 두고 사용자 언어를 도구까지 전달 | 영어 답변 15/15 |
| 외환 규정 답변 | 수령·송금 조항을 구분하지 못함 | 검색 결과에 조항 단위 라벨을 붙이고 위험 문장 검사기 추가 | 위험 오답 7건 → 0건 |

## 기술 스택

| 구분 | 사용 |
| --- | --- |
| 언어, 프레임워크 | Python 3.11, FastAPI, Pydantic |
| LLM, 에이전트 | Anthropic Claude, LangGraph, LangChain |
| 임베딩, 검색 | BGE-M3 (sentence-transformers), MongoDB Atlas Vector Search |
| 문서 파싱 | PDF, HWP, HWPX |
| OCR | PaddleOCR |
| 큐, 저장소 | RabbitMQ (aio-pika), Cloudflare R2 (boto3) |
| 개발 도구 | uv, Ruff, Mypy, pytest, pre-commit |
| 배포 | Docker, GitHub Actions |

## 폴더 구조

```
ai/
├── app/
│   ├── api/             내부 API 라우트, 토큰 검증
│   ├── chat/            챗봇 그래프, 도구, 프롬프트
│   ├── credit/          대안신용 계산
│   ├── finance/         금융 분석
│   ├── finance_policy/  금융 정책 검색, 답변 가드
│   ├── products/        금융상품 추천
│   ├── rag/             문서 추출, 청킹, 임베딩, 검색
│   ├── visa/            E-7-4 점수 엔진, 로드맵
│   ├── integrations/    Spring, R2, 법령 API 연동
│   └── worker/          정책 문서 적재 Worker
├── models/alt_credit/   대안신용 V9 모형
├── scripts/             적재, 평가, 측정 스크립트
├── specs/               설계 문서, 평가 질문과 결과
├── tests/               테스트
└── playground/          실험용 화면
ocr/                     외국인등록증 OCR Worker
data/rag_documents/      검색용 원문 (비자 정책, 신한 상품 설명서)
```

## 개발 준비

Python 서버의 기준 경로는 `ai/`입니다. 앱(`ai/app/`), 테스트(`ai/tests/`), Docker 이미지(`ai/Dockerfile`)가 있습니다.

Git과 [uv](https://docs.astral.sh/uv/getting-started/installation/)를 설치한 뒤 저장소 루트에서 실행합니다.
로컬 DB와 큐를 실행하려면 Docker Desktop도 설치하고 실행해야 합니다.

```powershell
uv python install 3.11
uv sync --project ai --locked --group dev
uv run --project ai --no-sync python --version
```

의존성은 `ai/.venv/`에 설치됩니다. 별도 가상환경 활성화는 필요하지 않습니다.
잠금 파일 설치가 실패하면 원인을 확인하고, 실패를 피하려고 잠금 파일을 재생성하지 않습니다.
CUDA와 임베딩 모델 가중치는 기본 환경설정에 포함되지 않습니다.

## 환경 변수

아래 PowerShell 명령은 환경 파일이 없을 때만 예시를 복사합니다.
이미 파일이 있다면 보존하고 예시와 비교해 누락된 키를 확인합니다.

```powershell
if (!(Test-Path .env)) { Copy-Item .env.example .env }
if (!(Test-Path ai/.env)) { Copy-Item ai/.env.example ai/.env }
```

복사 후 로컬 파일을 편집합니다. 예시 비밀번호와 외부 키의 자리표시자는 실제 설정으로 교체해야 합니다.
환경 파일은 Git에서 제외되며 키·비밀번호·연결 문자열을 로그나 커밋에 포함하지 않습니다.

| 파일 | 설정 |
| --- | --- |
| `.env` | AI compose 설정: 컨테이너용 MongoDB·RabbitMQ URI, AI 포트. `compose.full.yml` 사용 시 인프라 계정(백엔드 `.env`와 같은 값) |
| `ai/.env` | 호스트용 MongoDB·RabbitMQ URI, Spring 주소, Anthropic·R2 설정 |

DB·큐 비밀번호는 충분히 긴 임의 값으로 정하고 연결 URI의 자격 증명과 일치시킵니다.
URI에 넣는 사용자 이름과 비밀번호는 percent-encoding합니다.

호스트에서 실행할 때 MongoDB는 `localhost`와 `.env`의 `MONGODB_PORT`(기본 27017),
RabbitMQ는 `localhost:5672`와 기본 vhost `/`를 사용합니다.
MongoDB URI에는 `authSource=admin&directConnection=true`를 지정합니다.
컨테이너에서는 각각 `mongodb`, `rabbitmq`를 사용하며 Compose의 루트 `.env` 연결 설정이
`ai/.env` 값을 덮어씁니다. Spring은 호스트 실행 시 `http://localhost:8080`,
컨테이너에서는 `http://host.docker.internal:8080`을 사용합니다.

외부 연결에 필요한 설정은 `ANTHROPIC_API_KEY`, `SPRING_INTERNAL_API_KEY`,
`R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`입니다.
이 값 없이도 의존성 설치와 기본 품질 검사를 수행할 수 있습니다.

## 품질 검사와 푸시 훅

```powershell
uv run --project ai --no-sync pre-commit validate-config
uv run --project ai python scripts/check_ai.py
uv run --project ai --no-sync pre-commit install
uv run --project ai --no-sync pre-commit run --all-files --hook-stage pre-push
```

훅은 개발자마다 설치하며 **푸시 직전** 실행됩니다. 기존 훅이나 `core.hooksPath`가 있다면
충돌을 먼저 확인합니다. 공통 검사기는 Ruff 포맷·린트와 Mypy를 실행하고,
앱 소스가 생기면 앱 타입 검사와 단위 테스트를 요구합니다.
앱과 테스트가 모두 없는 `BOOTSTRAP` 상태는 앱 동작 검증이나 테스트 통과를 의미하지 않습니다.
단위 테스트는 `unit` 마커를 사용하며 실제 LLM·DB·네트워크에 접근하지 않습니다.

주요 패키지는 모델을 로드하지 않고 import만 확인할 수 있습니다.

```powershell
uv run --project ai --no-sync python -c "import fastapi, pydantic, langchain, langgraph, pymongo, sentence_transformers, aio_pika, boto3; print('Dependency imports OK')"
```

## 커밋 메시지 템플릿

저장소를 클론한 뒤 개발자마다 한 번씩 루트의 `.gitmessage`를 연결합니다.
이 설정은 현재 로컬 저장소에만 적용됩니다.

```powershell
git config --local commit.template .gitmessage
git commit
```

편집기가 열리면 맨 위 빈 줄에 `chore: 개발 환경 초기 설정`처럼 제목을 작성하고
저장한 뒤 종료합니다. `#`으로 시작하는 줄은 안내 주석이므로 커밋 메시지에 포함되지 않습니다.
제목을 작성하지 않으면 빈 메시지로 판단되어 커밋이 취소됩니다.
`git commit -m "메시지"`를 사용하면 템플릿 편집 없이 바로 메시지를 지정합니다.

## 로컬 인프라와 AI 서버

인프라(MySQL·RabbitMQ·MongoDB Atlas Local)는 **백엔드 저장소의 `docker-compose.yml` 하나에만** 정의합니다.
백엔드 compose가 공유 네트워크 `hanstep-net`을 만들고, 이 저장소의 compose는 그 네트워크에 붙어 AI 서버만 띄웁니다.

**방법 A — 나눠서 시작**

```bash
# 백엔드 저장소에서 인프라 시작
cd ../hanstep-backend && docker compose up -d
# 이 저장소에서 AI 서버 시작
cd ../hanstep-AI && docker compose up -d
```

**방법 B — 한 번에 시작** (`../hanstep-backend`에 백엔드 저장소를 클론해 둔 경우)

```bash
gh repo clone hanstep-lab/hanstep-backend ../hanstep-backend   # 최초 1회
docker compose -f compose.full.yml up -d
docker compose -f compose.full.yml ps
```

`compose.full.yml`은 백엔드 compose를 그대로 불러오므로 인프라 정의는 여전히 한 곳에만 있습니다.
B에서는 두 compose의 변수를 모두 이 저장소 루트 `.env`에서 읽으므로, `.env.example` 아래쪽의 인프라 계정 값을 백엔드 `.env`와 같게 맞춥니다.

- 루트 `.env`의 `MONGO_URI`·`RABBITMQ_URL`은 컨테이너 주소(`mongodb:27017`, `rabbitmq:5672`)이고,
  `ai/.env`의 같은 키는 호스트에서 직접 실행할 때 쓰는 `localhost` 주소입니다. 비밀번호는 백엔드 `.env`와 같아야 합니다.
- AI 서버 이미지는 CPU 전용 PyTorch를 써서 약 1.6GB입니다. BGE-M3 가중치(약 2.3GB)는 이미지에 넣지 않고
  `hf-cache` 볼륨에 처음 기동 때 한 번 내려받습니다. 받지 않고 띄우려면 `LOAD_MODEL_ON_STARTUP=false`.
- `ai-worker`는 RabbitMQ POLICY_INGEST 작업을 받아 정책 문서를 적재합니다(`python -m app.worker`).
  기본으로 뜨지 않으므로 `docker compose --profile worker up -d ai-worker`로 띄웁니다.
  큐 인자는 백엔드와 같아야 해서, retry 큐에 TTL이 붙어 있던 예전 브로커는 `policy.ingest.retry`를 지우고 시작합니다.
  재시도 대기는 `ai/.env`의 `RETRY_DELAYS_MS`(기본 `5000,15000,45000`)입니다.
- 기동 전에 호스트 포트 충돌을 확인합니다: MySQL 3307, RabbitMQ 5672·15672, MongoDB 27017, AI 8000.
  이미 쓰는 포트가 있으면 `.env`의 `MONGODB_PORT`·`MYSQL_PORT`·`AI_PORT`를 바꿉니다
  (RabbitMQ 포트는 현재 백엔드 compose에 고정돼 있습니다).
- Spring Boot는 호스트에서 실행하며 컨테이너에서는 `host.docker.internal:8080`으로 접근합니다.
- 이미지 빌드 전 디스크 여유 공간을 확인합니다(빌드 중 약 5GB 이상 필요).

중지할 때는 `docker compose stop`(B는 `-f compose.full.yml`)을 씁니다.
`docker compose down -v`는 저장된 DB 데이터와 모델 캐시를 삭제하므로 일반 중지 용도로 쓰지 않습니다.

## CI와 저장소 관리

GitHub Actions의 `AI quality` 워크플로는 Python 3.11과 잠금 의존성을 사용해 같은 검사기를 실행합니다.
PR·push·수동 실행을 지원하며 외부 API 키 없이 검사합니다.
저장소 관리자는 CI가 한 번 실행된 뒤 Settings → Rules → Rulesets에서 PR을 요구하고,
필수 상태 검사로 `AI quality gate`를 지정해야 합니다. 워크플로 파일만으로 병합 보호가 적용되지는 않습니다.

`ai/pyproject.toml`, `ai/uv.lock`, `.env.example` 두 파일, 검사기·훅·CI·Compose 설정은 공유 대상입니다.
로컬 AI 지침인 `AGENTS.md`와 `docs/`는 현재 `.gitignore` 정책에 따라 공유되지 않으며,
새로 클론한 환경에는 별도로 준비해야 합니다.

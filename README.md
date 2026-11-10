# Hanstep-AI
신한커리어넥스트 2회차 프로젝트 - 국내 거주 외국인의 금융생활 및 체류자격 성장을 지원하는 AI 플랫폼 '신한걸음'의 AI 레포지토리입니다.

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
- `ai-worker`는 RabbitMQ 작업 규격이 정해질 때까지 기본으로 뜨지 않습니다(`--profile worker`).
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

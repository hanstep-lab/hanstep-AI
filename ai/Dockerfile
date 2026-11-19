# syntax=docker/dockerfile:1
# hanstep-AI FastAPI server. Built from ai/ (compose build context).
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    # Model weights live in a volume (see docker-compose.yml), never in the image.
    HF_HOME=/models/huggingface \
    PATH=/opt/venv/bin:$PATH

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first for layer caching; only runtime deps (no dev/lint/test groups).
COPY pyproject.toml uv.lock ./
# The uv download cache stays in a BuildKit cache mount, not in an image layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY app ./app
COPY scripts ./scripts
COPY specs ./specs
# 대안신용 서빙 모델(계수·가정 JSON). 원본 v9_source.py는 학습용이라 넣지 않는다.
COPY models/alt_credit/alt_credit_v9.json ./models/alt_credit/alt_credit_v9.json

# 임베딩 캐시 폴더는 이미지에 미리 만든다. 볼륨을 붙일 때 Docker가 root 소유로 만들면
# hanstep 사용자가 캐시를 쓰지 못해 재시작마다 검색기를 처음부터 만든다(#98).
RUN useradd --create-home --uid 10001 hanstep \
    && mkdir -p /models/huggingface /app/artifacts/rag/embedding-cache \
    && chown -R hanstep:hanstep /models /app
USER hanstep

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

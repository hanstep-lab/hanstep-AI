FROM python:3.10-slim

# opencv, paddle이 쓰는 시스템 라이브러리 (slim 이미지에 없음)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# 모델 서버 연결 체크 생략, 로그 바로 출력
ENV PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pipeline/ pipeline/

# 빌드할 때 모델을 이미지에 넣어 둔다 (실행 시 다운로드 없음)
RUN python -W ignore -c "from pipeline.engine import load; load()"

# Worker(오지수). 모델 층 뒤에 두어 Worker 의존성·코드를 바꿔도 paddle 설치와 모델 다운로드를
# 다시 하지 않는다. pip check로 엔진 의존성과 충돌이 없는지 빌드 때 확인한다.
COPY requirements-worker.txt .
RUN pip install --no-cache-dir -r requirements-worker.txt && pip check

COPY worker/ worker/
CMD ["python", "-m", "worker"]

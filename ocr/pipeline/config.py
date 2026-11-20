"""OCR 엔진 설정(#110). 환경변수는 모델을 올릴 때 한 번 읽는다.

- OCR_DET_MODEL: 검출 모델. PP-OCRv5_server_det | PP-OCRv5_mobile_det. 비우면 기본값(mobile_det)
- OCR_MAX_SIDE: 긴 변이 이 값을 넘을 때만 비율을 지켜 줄인다. 0이면 줄이지 않는다. 2000 이하
  (앱이 긴 변 1500~2000px로 줄여 올려도 Worker가 다시 키우지 않게). 비우면 기본값(1280)
- OCR_CPU_THREADS: PaddleOCR CPU 스레드 수. 비우면 PaddleOCR 기본값(10). 컨테이너 CPU 상한(compose
  cpus)이나 물리 코어 수에 맞춘다. 결과(인식 글자)는 바뀌지 않고 시간만 바뀐다
인식 모델은 korean_PP-OCRv5_mobile_rec 고정이다.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

# 검출 모델 → modelVersion 라벨 조각. 인식은 mobile(mrec) 고정.
DET_MODELS = {"PP-OCRv5_server_det": "sdet", "PP-OCRv5_mobile_det": "mdet"}
REC_MODEL = "korean_PP-OCRv5_mobile_rec"
# #110 측정(앱과 같은 1500px 사진 3장): mobile_det는 server_det와 필드 정확도가 같고 5배쯤 빠르다.
# 1280으로 줄여도 8개 필드를 모두 읽고 1500 그대로보다 약 30% 빠르다.
DEFAULT_DET_MODEL = "PP-OCRv5_mobile_det"
DEFAULT_MAX_SIDE = 1280
MAX_SIDE_LIMIT = 2000
MAX_CPU_THREADS = 64


@dataclass(frozen=True)
class EngineConfig:
    det_model: str = DEFAULT_DET_MODEL
    max_side: int = DEFAULT_MAX_SIDE  # 0이면 줄이지 않는다
    cpu_threads: int | None = None  # None이면 PaddleOCR 기본값


def _int_env(environ: Mapping[str, str], name: str) -> int | None:
    raw = environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None


def config_from_env(environ: Mapping[str, str] = os.environ) -> EngineConfig:
    """환경변수 세 개를 읽는다. 잘못된 값이면 ValueError(Worker가 뜨지 않게)."""
    det_model = environ.get("OCR_DET_MODEL", "").strip() or DEFAULT_DET_MODEL
    if det_model not in DET_MODELS:
        raise ValueError(f"OCR_DET_MODEL must be one of {sorted(DET_MODELS)}")
    max_side = _int_env(environ, "OCR_MAX_SIDE")
    if max_side is None:
        max_side = DEFAULT_MAX_SIDE
    if not 0 <= max_side <= MAX_SIDE_LIMIT:
        raise ValueError(f"OCR_MAX_SIDE must be 0 (no resize) or 1..{MAX_SIDE_LIMIT}")
    cpu_threads = _int_env(environ, "OCR_CPU_THREADS")
    if cpu_threads is not None and not 1 <= cpu_threads <= MAX_CPU_THREADS:
        raise ValueError(f"OCR_CPU_THREADS must be 1..{MAX_CPU_THREADS}")
    return EngineConfig(det_model=det_model, max_side=max_side, cpu_threads=cpu_threads)


def model_version(config: EngineConfig) -> str:
    """실제 모델 조합이 드러나는 라벨. 예: ppocrv5-ko-mdet-mrec. BE 저장 한도 60자 안."""
    return f"ppocrv5-ko-{DET_MODELS[config.det_model]}-mrec"


def describe(config: EngineConfig) -> str:
    """Worker 시작 로그용 설정 요약(값만, 비밀값 없음)."""
    threads = config.cpu_threads if config.cpu_threads is not None else "default"
    return f"det={config.det_model} maxSide={config.max_side} cpuThreads={threads}"

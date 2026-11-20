"""등록증 앞면 OCR 엔진: 이미지 읽기(EXIF 회전) → 긴 변 축소 → PaddleOCR → 필드 추출.

설정(OCR_DET_MODEL, OCR_MAX_SIDE)은 pipeline/config.py, 전처리는 pipeline/image.py(#110).
"""

import json
import sys
import time
from dataclasses import dataclass

from paddleocr import PaddleOCR

from .config import REC_MODEL, EngineConfig, config_from_env, describe, model_version
from .extract import extract_fields
from .image import read_image, resize_to_max_side, size_text

MODEL_VERSION = model_version(EngineConfig())

_ocr = None
_config = EngineConfig()


def _get_ocr():
    """모델은 처음 한 번만 불러온다 (Worker가 요청마다 다시 로딩하지 않게)"""
    global _ocr
    if _ocr is None:
        # 스레드 수는 지정했을 때만 넘긴다(비우면 PaddleOCR 기본값).
        threads = {} if _config.cpu_threads is None else {"cpu_threads": _config.cpu_threads}
        _ocr = PaddleOCR(
            text_detection_model_name=_config.det_model,
            text_recognition_model_name=REC_MODEL,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            **threads,
        )
    return _ocr


def load():
    """Worker 시작할 때 한 번 호출. 설정을 읽고 모델을 미리 메모리에 올려둔다"""
    global _config, MODEL_VERSION
    if _ocr is None:
        _config = config_from_env()
        MODEL_VERSION = model_version(_config)
    _get_ocr()


def settings():
    """Worker 시작 로그용 설정 요약. 예: det=PP-OCRv5_mobile_det maxSide=1280 cpuThreads=2"""
    return describe(_config)


@dataclass(frozen=True)
class Analysis:
    lines: list
    original: str  # "가로x세로", EXIF 회전 뒤
    processed: str  # 추론에 넣은 크기
    resize_ms: int

    def timings(self):
        """로그용 값. 이미지 내용·인식 글자는 넣지 않는다."""
        return {"original": self.original, "processed": self.processed, "resize_ms": self.resize_ms}


def analyze(image_path):
    """이미지 → 인식된 글자와 크기 정보. 줄인 배열(BGR)을 PaddleOCR에 넘긴다."""
    image = read_image(image_path)
    started = time.perf_counter()
    resized = resize_to_max_side(image, _config.max_side)
    resize_ms = int((time.perf_counter() - started) * 1000)
    lines = []
    for res in _get_ocr().predict(resized):
        data = res.json
        data = data.get("res", data)
        lines += list(zip(data["rec_texts"], data["rec_scores"]))
    return Analysis(lines, size_text(image), size_text(resized), resize_ms)


def read_lines(image_path):
    """이미지 → [(인식된 글자, 신뢰도), ...]"""
    return analyze(image_path).lines


def recognize(front_path, back_path=None):
    """앞면(필수), 뒷면(선택) 이미지 경로 → 계약 형식 결과. 뒷면은 v0-lite에서 아직 사용하지 않음

    image(원본·처리 크기, resize_ms)는 Worker 로그용이다. Spring에는 보내지 않는다.
    """
    start = time.perf_counter()
    analysis = analyze(front_path)
    fields = extract_fields(analysis.lines)
    latency_ms = int((time.perf_counter() - start) * 1000)
    return {
        "modelVersion": MODEL_VERSION,
        "latencyMs": latency_ms,
        "fields": fields,
        "image": analysis.timings(),
    }


if __name__ == "__main__":
    load()  # 모델 로딩 시간은 처리시간에서 뺀다
    result = recognize(sys.argv[1])
    print(json.dumps(result, ensure_ascii=False, indent=2))

import json
import sys
import time

from paddleocr import PaddleOCR

from .extract import extract_fields

MODEL_VERSION = "ppocrv5-ko-v0lite"

_ocr = None


def _get_ocr():
    """모델은 처음 한 번만 불러온다 (Worker가 요청마다 다시 로딩하지 않게)"""
    global _ocr
    if _ocr is None:
        _ocr = PaddleOCR(
            lang="korean",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
    return _ocr

def load():
    """Worker 시작할 때 한 번 호출. 모델을 미리 메모리에 올려둔다"""
    _get_ocr()


def read_lines(image_path):
    """이미지 → [(인식된 글자, 신뢰도), ...]"""
    lines = []
    for res in _get_ocr().predict(image_path):
        data = res.json
        data = data.get("res", data)
        lines += list(zip(data["rec_texts"], data["rec_scores"]))
    return lines


def recognize(front_path, back_path=None):
    """앞면(필수), 뒷면(선택) 이미지 경로 → 계약 형식 결과. 뒷면은 v0-lite에서 아직 사용하지 않음"""
    start = time.perf_counter()
    lines = read_lines(front_path)
    fields = extract_fields(lines)
    latency_ms = int((time.perf_counter() - start) * 1000)
    return {"modelVersion": MODEL_VERSION, "latencyMs": latency_ms, "fields": fields}


if __name__ == "__main__":
    load()  # 모델 로딩 시간은 처리시간에서 뺀다
    result = recognize(sys.argv[1])
    print(json.dumps(result, ensure_ascii=False, indent=2))
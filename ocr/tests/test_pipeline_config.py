"""엔진 설정(OCR_DET_MODEL, OCR_MAX_SIDE)과 modelVersion 라벨(#110). paddle·opencv 없이 돈다."""

import pytest

from pipeline.config import (
    DEFAULT_DET_MODEL,
    DEFAULT_MAX_SIDE,
    MAX_SIDE_LIMIT,
    EngineConfig,
    config_from_env,
    describe,
    model_version,
)

pytestmark = pytest.mark.unit


def test_empty_env_uses_the_defaults():
    assert config_from_env({}) == EngineConfig(DEFAULT_DET_MODEL, DEFAULT_MAX_SIDE, None)
    assert config_from_env(
        {"OCR_DET_MODEL": " ", "OCR_MAX_SIDE": "", "OCR_CPU_THREADS": ""}
    ) == EngineConfig()
    # #110 측정으로 정한 기본값. 스레드는 비우면 PaddleOCR 기본값을 쓴다.
    assert (DEFAULT_DET_MODEL, DEFAULT_MAX_SIDE) == ("PP-OCRv5_mobile_det", 1280)
    assert EngineConfig().cpu_threads is None


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"OCR_DET_MODEL": "PP-OCRv5_mobile_det"}, EngineConfig("PP-OCRv5_mobile_det")),
        ({"OCR_DET_MODEL": "PP-OCRv5_server_det", "OCR_MAX_SIDE": "1280"},
         EngineConfig("PP-OCRv5_server_det", 1280)),
        ({"OCR_MAX_SIDE": "0"}, EngineConfig(max_side=0)),  # 줄이지 않음
        ({"OCR_MAX_SIDE": " 2000 "}, EngineConfig(max_side=2000)),
        ({"OCR_CPU_THREADS": "2"}, EngineConfig(cpu_threads=2)),
        ({"OCR_CPU_THREADS": " 4 "}, EngineConfig(cpu_threads=4)),
    ],
)
def test_env_values_are_parsed(env, expected):
    assert config_from_env(env) == expected


@pytest.mark.parametrize(
    "env",
    [
        {"OCR_DET_MODEL": "PP-OCRv6_medium_det"},  # 고르지 않은 모델
        {"OCR_DET_MODEL": "pp-ocrv5_mobile_det"},  # 대소문자도 정확히
        {"OCR_MAX_SIDE": "1600px"},
        {"OCR_MAX_SIDE": "-1"},
        {"OCR_MAX_SIDE": str(MAX_SIDE_LIMIT + 1)},  # 앱이 2000px로 줄여 올린다
        {"OCR_CPU_THREADS": "0"},
        {"OCR_CPU_THREADS": "two"},
        {"OCR_CPU_THREADS": "65"},
    ],
)
def test_bad_values_stop_the_worker(env):
    with pytest.raises(ValueError):
        config_from_env(env)


def test_label_shows_the_model_pair():
    assert model_version(EngineConfig("PP-OCRv5_server_det")) == "ppocrv5-ko-sdet-mrec"
    assert model_version(EngineConfig("PP-OCRv5_mobile_det")) == "ppocrv5-ko-mdet-mrec"
    # BE documents.ocr_model_version VARCHAR(60), OcrResultRequest.modelVersion @Size(max = 60)
    assert len(model_version(EngineConfig("PP-OCRv5_mobile_det"))) <= 60


def test_settings_summary_for_the_start_log():
    assert describe(EngineConfig()) == "det=PP-OCRv5_mobile_det maxSide=1280 cpuThreads=default"
    assert describe(EngineConfig("PP-OCRv5_server_det", 0, 2)) == (
        "det=PP-OCRv5_server_det maxSide=0 cpuThreads=2"
    )

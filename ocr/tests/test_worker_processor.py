"""OCR 처리 흐름: 원본 조회 → R2 임시 파일 → 엔진 → 결과 보고. 임시 파일 삭제와 로그 내용."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest
from worker_fakes import (
    FULL_REGISTRATION_NO,
    IMAGE,
    IMAGE_SHA256,
    OBJECT_KEY,
    FakeDelivery,
    FakeEngine,
    FakePublisher,
    FakeSpring,
    FakeStore,
    engine_result,
    make_handler,
    run,
)

from worker.handler import JobFailure, JobMessage
from worker.processor import OcrDeps, OcrProcessor
from worker.r2 import ImageTooLarge
from worker.retry import RETRY_COUNT_HEADER
from worker.spring_client import DocumentSource, SpringApiError

pytestmark = pytest.mark.unit

MESSAGE = JobMessage.model_validate(
    {
        "schemaVersion": 1,
        "jobId": 7,
        "jobType": "OCR",
        "resourceType": "DOCUMENT",
        "resourceId": 42,
        "requestId": "req_abc",
    }
)


def processor(
    tmp_path: Path,
    spring: FakeSpring | None = None,
    store: FakeStore | None = None,
    engine: FakeEngine | None = None,
) -> tuple[OcrProcessor, FakeSpring, FakeStore, FakeEngine]:
    spring = spring or FakeSpring()
    store = store or FakeStore()
    engine = engine or FakeEngine()
    deps = OcrDeps(spring=spring, store=store, recognize=engine, temp_dir=tmp_path)
    return OcrProcessor(deps), spring, store, engine


def run_failing(proc: OcrProcessor) -> JobFailure:
    with pytest.raises(JobFailure) as caught:
        proc(MESSAGE)
    return caught.value


# ---------- 정상 처리 ----------


def test_downloads_to_temp_file_recognizes_and_reports(tmp_path: Path) -> None:
    proc, spring, store, engine = processor(tmp_path)

    proc(MESSAGE)

    assert spring.sources == [42]
    assert store.keys == [OBJECT_KEY]
    [(path, existed, content)] = engine.calls
    assert existed and content == IMAGE  # 엔진은 받은 원본 그대로를 로컬 파일로 읽는다
    assert Path(path).parent == tmp_path and path.endswith(".jpg")
    [(document_id, body, request_id)] = spring.results
    assert (document_id, request_id) == (42, "req_abc")
    assert body["documentId"] == 42 and body["side"] == "FRONT"
    assert [f["fieldKey"] for f in body["fields"]] == [
        "nameEn",
        "nationality",
        "birthDate",
        "visaType",
        "stayExpiry",
        "registrationNo",
    ]
    assert list(tmp_path.iterdir()) == []  # 임시 파일 삭제


def test_callback_body_never_contains_full_registration_number(tmp_path: Path) -> None:
    engine = FakeEngine(engine_result(REGISTRATION_NO=FULL_REGISTRATION_NO))
    proc, spring, _, _ = processor(tmp_path, engine=engine)

    proc(MESSAGE)

    [(_, body, _)] = spring.results
    serialized = json.dumps(body)
    assert "5123456" not in serialized
    registration = next(f for f in body["fields"] if f["fieldKey"] == "registrationNo")
    assert registration == {
        "fieldKey": "registrationNo",
        "value": None,
        "confidence": None,
        "isMasked": True,
    }


def test_png_key_keeps_png_suffix(tmp_path: Path) -> None:
    source = DocumentSource(objectKey="arc/front/a.PNG", side="FRONT", sha256=IMAGE_SHA256)
    proc, _, _, engine = processor(tmp_path, spring=FakeSpring(source=source))

    proc(MESSAGE)

    assert engine.calls[0][0].endswith(".png")


# ---------- 임시 파일은 실패해도 지운다 ----------


@pytest.mark.parametrize(
    ("store", "engine", "code", "transient"),
    [
        pytest.param(
            FakeStore(error=ConnectionError("R2 responded 503")),
            None,
            "SOURCE_FETCH_FAILED",
            True,
            id="fetch-connection-error",
        ),
        pytest.param(
            FakeStore(error=LookupError("R2 object not found")),
            None,
            "SOURCE_MISSING",
            False,
            id="fetch-not-found",
        ),
        pytest.param(
            FakeStore(error=ImageTooLarge("too big")), None, "INVALID_IMAGE", False, id="too-large"
        ),
        pytest.param(FakeStore(data=b"other bytes"), None, "HASH_MISMATCH", False, id="hash"),
        pytest.param(
            None, FakeEngine(error=RuntimeError("paddle")), "OCR_FAILED", False, id="engine-error"
        ),
        pytest.param(
            None,
            FakeEngine(
                engine_result(NAME_EN=None, NATIONALITY=None, VISA_TYPE=None, REGISTRATION_NO=None)
            ),
            "OCR_FAILED",
            False,
            id="no-required-field",
        ),
    ],
)
def test_temp_file_is_deleted_on_failure(
    tmp_path: Path,
    store: FakeStore | None,
    engine: FakeEngine | None,
    code: str,
    transient: bool,
) -> None:
    proc, spring, _, _ = processor(tmp_path, store=store, engine=engine)

    failure = run_failing(proc)

    assert (failure.code, failure.transient) == (code, transient)
    assert list(tmp_path.iterdir()) == []
    assert spring.results == []  # 결과를 보내지 않는다


def test_hash_mismatch_does_not_call_engine(tmp_path: Path) -> None:
    proc, _, _, engine = processor(tmp_path, store=FakeStore(data=b"other bytes"))

    run_failing(proc)

    assert engine.calls == []


def test_temp_file_is_deleted_when_callback_fails(tmp_path: Path) -> None:
    spring = FakeSpring(result_error=SpringApiError(503, "INTERNAL_ERROR"))
    proc, _, _, engine = processor(tmp_path, spring=spring)

    failure = run_failing(proc)

    assert (failure.code, failure.transient) == ("SPRING_UNAVAILABLE", True)
    assert len(engine.calls) == 1
    assert list(tmp_path.iterdir()) == []


# ---------- 원본 위치 ----------


def test_document_without_object_key_is_source_missing(tmp_path: Path) -> None:
    source = DocumentSource(objectKey=None, side="FRONT", sha256=IMAGE_SHA256)
    proc, _, store, _ = processor(tmp_path, spring=FakeSpring(source=source))

    failure = run_failing(proc)

    assert (failure.code, failure.transient) == ("SOURCE_MISSING", False)
    assert store.keys == []


def test_back_side_document_is_not_processed(tmp_path: Path) -> None:
    source = DocumentSource(objectKey="arc/back/a.jpg", side="BACK", sha256=IMAGE_SHA256)
    proc, _, store, _ = processor(tmp_path, spring=FakeSpring(source=source))

    failure = run_failing(proc)

    assert (failure.code, failure.transient) == ("UNSUPPORTED_SIDE", False)
    assert store.keys == []


@pytest.mark.parametrize(
    ("error", "code", "transient"),
    [
        (SpringApiError(503, "INTERNAL_ERROR"), "SPRING_UNAVAILABLE", True),
        (httpx.ConnectError("refused"), "SPRING_UNAVAILABLE", True),
        (SpringApiError(404, "RESOURCE_NOT_FOUND"), "SPRING_REJECTED", False),
        (SpringApiError(200, "INVALID_BODY"), "SPRING_REJECTED", False),
    ],
)
def test_source_lookup_errors_are_classified(
    tmp_path: Path, error: Exception, code: str, transient: bool
) -> None:
    proc, _, store, _ = processor(tmp_path, spring=FakeSpring(source_error=error))

    failure = run_failing(proc)

    assert (failure.code, failure.transient) == (code, transient)
    assert store.keys == []


def test_callback_conflict_is_rejected_without_retry(tmp_path: Path) -> None:
    spring = FakeSpring(result_error=SpringApiError(409, "INVALID_STATE"))
    proc, _, _, _ = processor(tmp_path, spring=spring)

    failure = run_failing(proc)

    assert (failure.code, failure.transient) == ("SPRING_REJECTED", False)


# ---------- Worker 전체 흐름(handler + processor) ----------


def test_end_to_end_success_acks_and_logs_only_ids_and_timings(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    proc, spring, _, _ = processor(tmp_path)
    delivery = FakeDelivery()

    with caplog.at_level(logging.DEBUG):
        run(make_handler(spring, proc).handle(delivery))

    assert delivery.outcome == [("ack",)]
    assert len(spring.results) == 1 and spring.failed == []
    assert "jobId=7 documentId=42" in caplog.text
    assert "latencyMs=640" in caplog.text and "timings=" in caplog.text
    # 원본·처리 크기와 resize_ms(#110)가 단계별 시간과 함께 남는다.
    assert "'original': '4032x3024', 'processed': '1600x1200', 'resize_ms': 41" in caplog.text
    assert "modelVersion=ppocrv5-ko-mdet-mrec" in caplog.text
    for secret in ("REYES", "PHL", "1995-08-15", "E-9", "950815", OBJECT_KEY, str(tmp_path)):
        assert secret not in caplog.text


@pytest.mark.parametrize(
    "image",
    [
        None,
        "4032x3024",
        {"original": "MARK ANTHONY REYES", "processed": "1600 x 1200", "resize_ms": "41"},
        {"original": "4032x3024", "resize_ms": True, "lines": ["MARK ANTHONY REYES"]},
    ],
)
def test_only_well_formed_image_sizes_reach_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, image: object
) -> None:
    raw = engine_result()
    raw["image"] = image
    proc, _, _, _ = processor(tmp_path, engine=FakeEngine(result=raw))

    with caplog.at_level(logging.INFO):
        proc(MESSAGE)

    assert "REYES" not in caplog.text and "1600 x 1200" not in caplog.text
    assert "resize_ms" not in caplog.text
    if isinstance(image, dict) and image.get("original") == "4032x3024":
        assert "'original': '4032x3024'" in caplog.text


def test_end_to_end_ocr_failed_goes_to_dlq_without_retry(tmp_path: Path) -> None:
    engine = FakeEngine(
        engine_result(NAME_EN=None, NATIONALITY=None, VISA_TYPE=None, REGISTRATION_NO=None)
    )
    proc, spring, _, _ = processor(tmp_path, engine=engine)
    publisher, delivery = FakePublisher(), FakeDelivery()

    run(make_handler(spring, proc, publisher).handle(delivery))

    assert spring.failed == [(7, "OCR_FAILED", "no required field recognized")]
    assert publisher.published == []
    assert delivery.outcome == [("reject", "False")]


def test_end_to_end_fetch_error_is_retried_and_temp_file_removed(tmp_path: Path) -> None:
    store = FakeStore(error=ConnectionError("R2 unavailable: ConnectTimeoutError"))
    proc, spring, _, _ = processor(tmp_path, store=store)
    publisher, delivery = FakePublisher(), FakeDelivery()

    run(make_handler(spring, proc, publisher).handle(delivery))

    assert [(p["headers"][RETRY_COUNT_HEADER], p["expiration"]) for p in publisher.published] == [
        (1, "5000")
    ]
    assert delivery.outcome == [("ack",)]
    assert spring.failed == []
    assert list(tmp_path.iterdir()) == []

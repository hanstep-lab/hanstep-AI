"""OCR 처리: 원본 위치 조회 → R2 원본을 임시 파일로 → 해시 대조 → 엔진 → 변환 → 결과 보고.

실패는 단계별 코드로 :class:`JobFailure`를 던진다. 연결 오류만 일시 오류(transient)다.
임시 파일은 성공·실패와 관계없이 엔진을 부른 뒤 바로 지운다.
로그에는 jobId·documentId·latencyMs·단계별 시간만 남기고 이미지·OCR 값·원본 키는 넣지 않는다.
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Protocol

from worker.fields import SIDE, build_ocr_result
from worker.handler import JobFailure, JobMessage, spring_failure
from worker.r2 import ArcImageStore, ImageTooLarge
from worker.spring_client import DocumentSource

logger = logging.getLogger(__name__)

# 엔진에 넘길 임시 파일 확장자. BE는 JPEG·PNG만 받는다(arc/{side}/{UUID}.jpg|png).
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}

Recognize = Callable[[str], Mapping[str, Any]]


class DocumentClient(Protocol):
    """SpringInternalClient 중 원본 위치 조회와 OCR 결과 보고."""

    def document_source(
        self, document_id: int, *, timeout: float = 10.0, request_id: str | None = None
    ) -> DocumentSource: ...

    def ocr_result(
        self, document_id: int, body: dict[str, Any], *, request_id: str | None = None
    ) -> None: ...


@dataclass
class OcrDeps:
    spring: DocumentClient
    store: ArcImageStore
    # pipeline.engine.recognize(앞면 경로). 모델은 Worker 시작 때 한 번 올린다.
    recognize: Recognize
    # 임시 파일 위치. None이면 시스템 임시 폴더.
    temp_dir: Path | None = None


def _suffix(object_key: str) -> str:
    suffix = Path(object_key).suffix.lower()
    return suffix if suffix in _IMAGE_SUFFIXES else ".img"


class OcrProcessor:
    """작업 메시지 하나를 처리한다. 같은 결과를 다시 보내도 Spring이 204로 받는다."""

    def __init__(self, deps: OcrDeps) -> None:
        self._deps = deps

    def __call__(self, message: JobMessage) -> None:
        deps = self._deps
        document_id = message.resource_id
        tag = f"jobId={message.job_id} documentId={document_id} requestId={message.request_id}"
        timings: dict[str, int] = {}

        with _timed(timings, "lookup_ms"):
            try:
                source = deps.spring.document_source(document_id, request_id=message.request_id)
            except Exception as exc:
                failure = spring_failure(exc, "lookup")
                if failure is None:
                    raise
                raise failure from exc
        if not source.object_key:
            raise JobFailure("SOURCE_MISSING", transient=False, reason="document has no object key")
        if source.side != SIDE:
            raise JobFailure(
                "UNSUPPORTED_SIDE", transient=False, reason=f"side {source.side} is not {SIDE}"
            )

        raw = self._recognize(source, timings)
        result = build_ocr_result(document_id, raw)

        with _timed(timings, "callback_ms"):
            try:
                deps.spring.ocr_result(document_id, result.to_json(), request_id=message.request_id)
            except Exception as exc:
                failure = spring_failure(exc, "callback")
                if failure is None:
                    raise
                raise failure from exc

        logger.info(
            "%s ocr reported modelVersion=%s latencyMs=%d timings=%s",
            tag,
            result.model_version,
            result.latency_ms,
            timings,
        )

    def _recognize(self, source: DocumentSource, timings: dict[str, int]) -> Mapping[str, Any]:
        """원본을 임시 파일로 받아 엔진에 넘긴다. 임시 파일은 여기서 반드시 지운다."""
        deps = self._deps
        assert source.object_key is not None
        fd, name = tempfile.mkstemp(
            prefix="ocr-", suffix=_suffix(source.object_key), dir=deps.temp_dir
        )
        path = Path(name)
        try:
            with _timed(timings, "fetch_ms"), os.fdopen(fd, "wb") as out:
                try:
                    digest = deps.store.download(source.object_key, out)
                except LookupError as exc:
                    raise JobFailure(
                        "SOURCE_MISSING", transient=False, reason="R2 object not found"
                    ) from exc
                except ImageTooLarge as exc:
                    raise JobFailure(
                        "INVALID_IMAGE", transient=False, reason="R2 object exceeds size limit"
                    ) from exc
                except ConnectionError as exc:
                    raise JobFailure(
                        "SOURCE_FETCH_FAILED", transient=True, reason=f"fetch: {exc}"
                    ) from exc
            if digest != source.sha256:
                raise JobFailure(
                    "HASH_MISMATCH", transient=False, reason="R2 bytes differ from sha256"
                )
            with _timed(timings, "ocr_ms"):
                try:
                    return deps.recognize(str(path))
                except Exception as exc:
                    # 엔진·이미지 디코딩 오류. 같은 이미지로 다시 해도 결과가 같다
                    raise JobFailure(
                        "OCR_FAILED", transient=False, reason=f"engine: {type(exc).__name__}"
                    ) from exc
        finally:
            path.unlink(missing_ok=True)


@contextmanager
def _timed(timings: dict[str, int], name: str) -> Iterator[None]:
    started = monotonic()
    try:
        yield
    finally:
        timings[name] = int((monotonic() - started) * 1000)

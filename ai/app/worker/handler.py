"""작업 메시지 하나의 처리 흐름(작업 종류 공통).

1. 본문 검증. 틀리면 job_failed 없이 reject(requeue=False) → DLQ.
2. job_started(INT-08) 결과와 redelivered로 처리할지 정한다(:func:`decide`).
3. 처리 성공 → ack. 일시 오류 → retry 큐로 재발행(x-retry-count +1, expiration) 후 ack,
   재시도를 다 썼으면 job_failed 후 DLQ. 재시도해도 안 되는 실패 → job_failed 후 DLQ.

Spring 호출과 처리 함수는 동기 코드라 스레드에서 돌린다(이벤트 루프가 heartbeat를 계속 보낸다).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

from app.integrations.spring_client import PolicyFailureCode, SpringApiError, StartResult
from app.worker.retry import retry_count, retry_expiration, retry_headers

logger = logging.getLogger(__name__)


class JobMessage(BaseModel):
    """BE AiJobMessage(JSON). 필드를 바꾸면 BE가 schemaVersion을 올린다."""

    model_config = ConfigDict(alias_generator=to_camel, frozen=True, extra="forbid")

    schema_version: Literal[1]
    job_id: int = Field(ge=1)
    job_type: str
    resource_type: str
    resource_id: int = Field(ge=1)
    request_id: str | None = None


class InvalidMessage(Exception):
    """처리할 수 없는 메시지(형식 오류, 다른 작업 종류). 재시도하지 않고 DLQ로 보낸다."""


def parse_message(body: bytes, *, job_type: str, resource_type: str) -> JobMessage:
    try:
        message = JobMessage.model_validate(json.loads(body))
    except (ValueError, ValidationError) as exc:
        # ValidationError 문자열에는 입력값이 들어가므로 오류 위치만 남긴다
        fields = (
            [".".join(str(p) for p in e["loc"]) for e in exc.errors()]
            if isinstance(exc, ValidationError)
            else ["body is not JSON"]
        )
        raise InvalidMessage(f"invalid fields: {fields}") from exc
    if message.job_type != job_type or message.resource_type != resource_type:
        raise InvalidMessage(
            f"unexpected jobType/resourceType {message.job_type}/{message.resource_type}"
        )
    return message


class Decision(StrEnum):
    PROCESS = "PROCESS"
    SKIP = "SKIP"  # 처리하지 않고 ack


def decide(result: StartResult, *, redelivered: bool, retry_count: int) -> Decision:
    """작업 1(#38)의 Worker 규칙.

    - STARTED → 처리
    - ALREADY_FINISHED → ack
    - ALREADY_PROCESSING + redelivered(처리 중 Worker가 죽은 뒤 다시 온 메시지) → 이어서 처리
    - ALREADY_PROCESSING + not redelivered(중복 메시지) → ack

    retry 큐에서 돌아온 메시지(retry_count > 0)는 Worker가 일부러 다시 보낸 새 메시지라
    redelivered가 false다. 작업은 PROCESSING에 남아 있으므로 redelivered처럼 이어서 처리한다.
    """
    if result is StartResult.STARTED:
        return Decision.PROCESS
    if result is StartResult.ALREADY_FINISHED:
        return Decision.SKIP
    return Decision.PROCESS if redelivered or retry_count > 0 else Decision.SKIP


class JobFailure(Exception):
    """처리 실패. transient면 retry 큐로 다시 시도하고, 아니면 바로 job_failed 후 DLQ.

    reason은 INT-10 message로 Spring에 간다. 문서 본문·URL·개인정보를 넣지 않는다.
    """

    def __init__(self, code: PolicyFailureCode, *, transient: bool, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code: PolicyFailureCode = code
        self.transient = transient
        self.reason = reason


def spring_failure(exc: Exception, stage: str) -> JobFailure | None:
    """Spring 호출 오류 분류. 연결 실패·시간 초과·5xx·429는 일시 오류, 나머지 4xx는 아니다."""
    if isinstance(exc, httpx.TransportError):
        return JobFailure(
            "SPRING_UNAVAILABLE", transient=True, reason=f"{stage}: {type(exc).__name__}"
        )
    if isinstance(exc, SpringApiError):
        if exc.status == 0 or exc.status >= 500 or exc.status == 429:
            return JobFailure(
                "SPRING_UNAVAILABLE", transient=True, reason=f"{stage}: {exc.status} {exc.code}"
            )
        return JobFailure(
            "SPRING_REJECTED", transient=False, reason=f"{stage}: {exc.status} {exc.code}"
        )
    return None


class Delivery(Protocol):
    """aio-pika IncomingMessage 중 Worker가 쓰는 부분."""

    @property
    def body(self) -> bytes: ...

    @property
    def redelivered(self) -> bool | None: ...

    @property
    def headers(self) -> Mapping[str, Any]: ...

    @property
    def message_id(self) -> str | None: ...

    async def ack(self) -> None: ...

    async def reject(self, requeue: bool = False) -> None: ...

    async def nack(self, multiple: bool = False, requeue: bool = True) -> None: ...


class RetryPublisher(Protocol):
    async def publish_retry(
        self, body: bytes, *, headers: dict[str, Any], expiration: str, message_id: str | None
    ) -> None:
        """retry 큐로 persistent 재발행. 브로커가 받을 때까지 기다린다(publisher confirm)."""
        ...


class JobStatusReporter(Protocol):
    """SpringInternalClient 중 작업 상태 보고(INT-08, INT-10)."""

    def job_started(
        self, job_id: int, *, worker_id: str, started_at: datetime, request_id: str | None = None
    ) -> StartResult: ...

    def job_failed(
        self,
        job_id: int,
        *,
        error_code: PolicyFailureCode,
        message: str,
        request_id: str | None = None,
    ) -> None: ...


Processor = Callable[[JobMessage], None]


class JobHandler:
    """작업 큐 하나의 메시지를 처리한다. 처리 함수가 작업 종류별 일을 한다."""

    def __init__(
        self,
        *,
        job_type: str,
        resource_type: str,
        spring: JobStatusReporter,
        processor: Processor,
        publisher: RetryPublisher,
        retry_delays_ms: tuple[int, ...],
        worker_id: str,
    ) -> None:
        self._job_type = job_type
        self._resource_type = resource_type
        self._spring = spring
        self._processor = processor
        self._publisher = publisher
        self._delays = retry_delays_ms
        self._worker_id = worker_id

    async def handle(self, delivery: Delivery) -> None:
        try:
            message = parse_message(
                delivery.body, job_type=self._job_type, resource_type=self._resource_type
            )
        except InvalidMessage as exc:
            logger.warning("invalid message (messageId=%s) to DLQ: %s", delivery.message_id, exc)
            await delivery.reject(requeue=False)
            return
        count = retry_count(delivery.headers)
        redelivered = bool(delivery.redelivered)
        tag = f"jobId={message.job_id} requestId={message.request_id}"

        try:
            result = await asyncio.to_thread(
                self._spring.job_started,
                message.job_id,
                worker_id=self._worker_id,
                started_at=datetime.now(UTC),
                request_id=message.request_id,
            )
        except Exception as exc:  # noqa: BLE001 - 분류해서 재시도·DLQ로 보낸다
            failure = spring_failure(exc, "started") or JobFailure(
                "WORKER_ERROR", transient=False, reason=f"started: {type(exc).__name__}"
            )
            if failure.transient:
                await self._retry_or_fail(delivery, message, count, failure, tag)
            else:
                # 작업이 없거나(404) 모르는 409라 job_failed도 받아 주지 않는다
                logger.warning("%s job_started rejected (%s), to DLQ", tag, failure.reason)
                await delivery.reject(requeue=False)
            return

        decision = decide(result, redelivered=redelivered, retry_count=count)
        logger.info(
            "%s start=%s redelivered=%s retryCount=%d -> %s",
            tag,
            result.value,
            redelivered,
            count,
            decision.value,
        )
        if decision is Decision.SKIP:
            await delivery.ack()
            return

        try:
            await asyncio.to_thread(self._processor, message)
        except JobFailure as failure:
            if failure.transient:
                await self._retry_or_fail(delivery, message, count, failure, tag)
            else:
                await self._fail(delivery, message, failure, tag)
            return
        except Exception as exc:  # noqa: BLE001 - 분류하지 못한 오류도 FAILED로 남긴다
            logger.exception("%s unexpected worker error", tag)
            unexpected = JobFailure("WORKER_ERROR", transient=False, reason=type(exc).__name__)
            await self._fail(delivery, message, unexpected, tag)
            return
        await delivery.ack()

    async def _retry_or_fail(
        self,
        delivery: Delivery,
        message: JobMessage,
        count: int,
        failure: JobFailure,
        tag: str,
    ) -> None:
        expiration = retry_expiration(count, self._delays)
        if expiration is None:
            logger.warning("%s %s after %d retries", tag, failure.code, count)
            await self._fail(delivery, message, failure, tag)
            return
        try:
            await self._publisher.publish_retry(
                delivery.body,
                headers=retry_headers(delivery.headers, count + 1),
                expiration=expiration,
                message_id=delivery.message_id,
            )
        except Exception:  # noqa: BLE001 - 재발행 못 하면 원본을 되돌려 다시 받는다
            logger.exception("%s retry publish failed, requeue original", tag)
            await delivery.nack(requeue=True)
            return
        logger.info(
            "%s %s (%s), retry %d/%d after %sms",
            tag,
            failure.code,
            failure.reason,
            count + 1,
            len(self._delays),
            expiration,
        )
        await delivery.ack()

    async def _fail(
        self, delivery: Delivery, message: JobMessage, failure: JobFailure, tag: str
    ) -> None:
        try:
            await asyncio.to_thread(
                self._spring.job_failed,
                message.job_id,
                error_code=failure.code,
                message=failure.reason,
                request_id=message.request_id,
            )
        except Exception:  # noqa: BLE001 - 보고를 못 해도 메시지는 DLQ에 남긴다
            logger.exception("%s job_failed report failed", tag)
        logger.warning("%s failed %s (%s), to DLQ", tag, failure.code, failure.reason)
        await delivery.reject(requeue=False)

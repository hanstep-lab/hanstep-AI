"""Worker 프로세스: 설정 확인 → 엔진 모델 로드(한 번) → 클라이언트 준비 → RabbitMQ 소비.

aio-pika connect_robust로 연결이 끊기면 다시 붙고 큐 선언·소비를 되살린다. prefetch 1,
수동 ack라서 처리 중 프로세스가 죽으면 그 메시지는 redelivered=true로 다시 온다.
paddle 예측기는 모델 로드와 추론 모두 전용 스레드 하나에서만 부른다.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import socket
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from time import monotonic
from typing import Any, TypeVar

import aio_pika

from worker import topology
from worker.config import Settings, configure_logging, get_settings
from worker.handler import JobHandler, error_location
from worker.processor import OcrDeps, OcrProcessor
from worker.r2 import R2ArcImageStore
from worker.spring_client import SpringInternalClient

logger = logging.getLogger(__name__)

EXIT_NOT_CONFIGURED = 2

T = TypeVar("T")


def retry_message(
    body: bytes, *, headers: dict[str, Any], expiration: str, message_id: str | None
) -> aio_pika.Message:
    """retry 큐 재발행 메시지. persistent이고 expiration은 ms 문자열 그대로 나간다."""
    return aio_pika.Message(
        body,
        headers=headers,
        content_type="application/json",
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        message_id=message_id,
        expiration=timedelta(milliseconds=int(expiration)),
    )


class RabbitRetryPublisher:
    """hanstep.ai exchange로 retry 큐에 재발행한다(publisher confirm 채널)."""

    def __init__(self, exchange: aio_pika.abc.AbstractExchange, routing_key: str) -> None:
        self._exchange = exchange
        self._routing_key = routing_key

    async def publish_retry(
        self, body: bytes, *, headers: dict[str, Any], expiration: str, message_id: str | None
    ) -> None:
        message = retry_message(body, headers=headers, expiration=expiration, message_id=message_id)
        await self._exchange.publish(message, routing_key=self._routing_key, mandatory=True)


class EngineThread:
    """엔진 호출을 한 스레드로 모은다(paddle 예측기를 스레드 사이로 옮기지 않는다)."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocr-engine")

    def call(self, fn: Callable[..., T], *args: Any) -> T:
        return self._executor.submit(fn, *args).result()

    def close(self) -> None:
        self._executor.shutdown(wait=True)


@dataclass
class WorkerResources:
    processor: OcrProcessor
    spring: SpringInternalClient
    close: Callable[[], None]


def build_resources(settings: Settings) -> WorkerResources:
    """엔진 모델을 한 번 올리고 R2·Spring 클라이언트를 만든다."""
    # paddle은 무겁고 테스트에는 필요 없어서 여기서만 import한다.
    from pipeline import engine

    engine_thread = EngineThread()
    started = monotonic()
    engine_thread.call(engine.load)
    logger.info(
        "ocr model %s loaded in %.1fs (%s)",
        engine.MODEL_VERSION,
        monotonic() - started,
        engine_thread.call(engine.settings),
    )

    def recognize(front_path: str) -> Mapping[str, Any]:
        result: Mapping[str, Any] = engine_thread.call(engine.recognize, front_path)
        return result

    store = R2ArcImageStore(
        endpoint_url=settings.r2_endpoint_url,
        access_key_id=settings.r2_ocr_access_key_id.get_secret_value(),
        secret_access_key=settings.r2_ocr_secret_access_key.get_secret_value(),
        bucket=settings.r2_arc_bucket,
        region=settings.r2_region,
    )
    spring = SpringInternalClient(
        base_url=settings.spring_internal_api_url,
        api_key=settings.spring_internal_api_key.get_secret_value(),
        header=settings.internal_token_header,
    )
    processor = OcrProcessor(OcrDeps(spring=spring, store=store, recognize=recognize))

    def close() -> None:
        spring.close()
        engine_thread.close()

    return WorkerResources(processor=processor, spring=spring, close=close)


async def run(settings: Settings) -> None:
    resources = build_resources(settings)
    worker_id = settings.worker_id or f"ocr-worker-{socket.gethostname()}"
    try:
        connection = await aio_pika.connect_robust(settings.rabbitmq_url.get_secret_value())
    except BaseException:
        resources.close()
        raise
    try:
        channel = await connection.channel(publisher_confirms=True)
        await channel.set_qos(prefetch_count=1)
        route = topology.OCR
        exchange, queue = await topology.declare(channel, route)
        handler = JobHandler(
            spring=resources.spring,
            processor=resources.processor,
            publisher=RabbitRetryPublisher(exchange, route.retry_queue),
            retry_delays_ms=settings.retry_delays_ms,
            worker_id=worker_id,
        )

        async def on_message(message: aio_pika.abc.AbstractIncomingMessage) -> None:
            try:
                await handler.handle(message)
            except Exception as exc:  # noqa: BLE001 - ack 전 채널이 닫힌 경우 등. 메시지는 다시 온다
                logger.error(
                    "message handling aborted (messageId=%s): %s",
                    message.message_id,
                    error_location(exc),
                )

        await queue.consume(on_message, no_ack=False)
        logger.info(
            "worker %s consuming %s (prefetch 1, retry delays %s ms)",
            worker_id,
            route.routing_key,
            ",".join(str(d) for d in settings.retry_delays_ms),
        )
        await _wait_for_stop()
    finally:
        await connection.close()
        resources.close()


async def _wait_for_stop() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows: Ctrl+C는 KeyboardInterrupt
            pass
    await stop.wait()
    logger.info("stop signal received, closing connection")


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    missing = settings.missing()
    if missing:
        logger.error("worker not configured: %s", ", ".join(missing))
        return EXIT_NOT_CONFIGURED
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        logger.info("interrupted")
    return 0

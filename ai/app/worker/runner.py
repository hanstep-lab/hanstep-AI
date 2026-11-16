"""Worker 프로세스: 설정 확인 → 의존성 준비(임베더는 시작 때 한 번) → RabbitMQ 소비.

aio-pika connect_robust로 연결이 끊기면 다시 붙고 큐 선언·소비를 되살린다. prefetch 1,
수동 ack라서 처리 중 프로세스가 죽으면 그 메시지는 redelivered=true로 다시 온다.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from time import monotonic
from typing import Any

import aio_pika

from app.core.config import Settings, configure_logging, get_settings
from app.integrations.policy_gateway import UnconfiguredPolicyGateway
from app.integrations.r2 import build_object_stores
from app.integrations.spring_client import SpringInternalClient
from app.rag.embeddings import BgeM3Embedder
from app.rag.ingestion import IngestionDeps
from app.rag.repository import ChunkRepository
from app.worker import topology
from app.worker.handler import JobHandler
from app.worker.policy_ingest import DOMAIN, PolicyIngestDeps, PolicyIngestProcessor

logger = logging.getLogger(__name__)

EXIT_NOT_CONFIGURED = 2


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


def missing_settings(settings: Settings) -> list[str]:
    missing = []
    if not settings.rabbitmq_configured():
        missing.append("RABBITMQ_URL")
    if not settings.spring_configured():
        missing.append("SPRING_INTERNAL_API_URL/SPRING_INTERNAL_API_KEY")
    if not settings.mongo_configured():
        missing.append("MONGO_URI")
    if not settings.r2_configured():
        missing.append("R2_ENDPOINT_URL/R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY/R2_POLICY_BUCKET")
    return missing


@dataclass
class WorkerResources:
    processor: PolicyIngestProcessor
    spring: SpringInternalClient
    close: Callable[[], None]


def build_resources(settings: Settings) -> WorkerResources:
    """MongoDB·R2·Spring 클라이언트와 임베더를 만든다. 임베더는 여기서 한 번만 올린다."""
    from pymongo import MongoClient

    mongo_client: Any = MongoClient(
        settings.mongo_uri.get_secret_value(), serverSelectionTimeoutMS=5000, tz_aware=True
    )
    repository = ChunkRepository(mongo_client[settings.mongo_database], settings.mongo_collection)
    if repository.unique_index_status() != "match":
        mongo_client.close()
        raise RuntimeError(
            "chunk unique index missing; run scripts/create_atlas_indexes.py --apply"
        )

    embedder = BgeM3Embedder(
        settings.embedding_model,
        revision=settings.embedding_model_revision,
        device=settings.embedding_device,
        batch_size=settings.embedding_batch_size,
    )
    started = monotonic()
    embedder.load()
    logger.info(
        "embedding model %s loaded=%s in %.1fs",
        settings.embedding_model,
        embedder.loaded,
        monotonic() - started,
    )

    stores = build_object_stores(settings)
    spring = SpringInternalClient(
        base_url=settings.spring_internal_api_url,
        api_key=settings.spring_internal_api_key.get_secret_value(),
        header=settings.internal_token_header,
    )
    ingestion = IngestionDeps(
        repository=repository,
        # Worker는 MySQL 확인을 Spring 단건 조회로 하므로 게이트웨이를 쓰지 않는다
        gateway=UnconfiguredPolicyGateway(),
        object_stores=stores,
        embedder=embedder,
        token_counter=embedder.count_tokens,
        tokenizer_id=f"{settings.embedding_model}@{settings.embedding_model_revision or 'default'}",
        r2_prefix=settings.r2_policy_prefix,
        embedding_model=settings.embedding_model,
    )
    processor = PolicyIngestProcessor(
        PolicyIngestDeps(
            spring=spring,
            object_store=stores[DOMAIN],
            ingestion=ingestion,
            mongo_collection=settings.mongo_collection,
            embedding_revision=settings.embedding_model_revision,
            model_loaded=lambda: embedder.loaded,
        )
    )

    def close() -> None:
        spring.close()
        mongo_client.close()

    return WorkerResources(processor=processor, spring=spring, close=close)


async def run(settings: Settings) -> None:
    resources = build_resources(settings)
    worker_id = settings.worker_id or f"policy-worker-{socket.gethostname()}"
    connection = await aio_pika.connect_robust(settings.rabbitmq_url.get_secret_value())
    try:
        channel = await connection.channel(publisher_confirms=True)
        await channel.set_qos(prefetch_count=1)
        route = topology.POLICY_INGEST
        exchange, queue = await topology.declare(channel, route)
        handler = JobHandler(
            job_type="POLICY_INGEST",
            resource_type="POLICY_DOCUMENT",
            spring=resources.spring,
            processor=resources.processor,
            publisher=RabbitRetryPublisher(exchange, route.retry_queue),
            retry_delays_ms=settings.retry_delays_ms,
            worker_id=worker_id,
        )

        async def on_message(message: aio_pika.abc.AbstractIncomingMessage) -> None:
            try:
                await handler.handle(message)
            except Exception:  # noqa: BLE001 - ack 전 채널이 닫힌 경우 등. 메시지는 다시 온다
                logger.exception("message handling aborted (messageId=%s)", message.message_id)

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
    missing = missing_settings(settings)
    if missing:
        logger.error("worker not configured: %s", ", ".join(missing))
        return EXIT_NOT_CONFIGURED
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        logger.info("interrupted")
    return 0

"""AI 작업 큐 구성. hanstep-backend AiJobRabbitConfiguration·AiJobRoute와 인자까지 같게 선언한다.

인자가 하나라도 다르면 브로커가 PRECONDITION_FAILED로 채널을 닫는다. retry 큐에는 큐 TTL이
없고(backend #74), 대기 시간은 Worker가 재발행할 때 메시지 expiration으로 붙인다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aio_pika

EXCHANGE = "hanstep.ai"


@dataclass(frozen=True)
class JobRoute:
    """작업 종류 하나의 큐 세 개: 작업 큐, retry 큐, DLQ."""

    routing_key: str

    @property
    def retry_queue(self) -> str:
        return f"{self.routing_key}.retry"

    @property
    def dead_letter_queue(self) -> str:
        return f"{self.routing_key}.dlq"

    def queue_arguments(self) -> dict[str, dict[str, Any]]:
        """큐 이름별 arguments(BE QueueBuilder와 같은 값)."""
        return {
            # Worker가 reject(requeue=False)하면 DLQ로 간다
            self.routing_key: {
                "x-dead-letter-exchange": EXCHANGE,
                "x-dead-letter-routing-key": self.dead_letter_queue,
            },
            # 메시지 expiration이 지나면 작업 큐로 돌아간다
            self.retry_queue: {
                "x-dead-letter-exchange": EXCHANGE,
                "x-dead-letter-routing-key": self.routing_key,
            },
            self.dead_letter_queue: {},
        }


POLICY_INGEST = JobRoute("policy.ingest")


async def declare(
    channel: aio_pika.abc.AbstractChannel, route: JobRoute
) -> tuple[aio_pika.abc.AbstractExchange, aio_pika.abc.AbstractQueue]:
    """exchange와 큐 세 개, 바인딩을 선언하고 (exchange, 작업 큐)를 돌려준다.

    RabbitMQ 4는 durable이 아닌 공유 큐를 거부하므로 모두 durable로 선언한다.
    """
    exchange = await channel.declare_exchange(EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True)
    work: aio_pika.abc.AbstractQueue | None = None
    for name, arguments in route.queue_arguments().items():
        queue = await channel.declare_queue(name, durable=True, arguments=arguments or None)
        await queue.bind(exchange, routing_key=name)
        if name == route.routing_key:
            work = queue
    assert work is not None
    return exchange, work

"""FastAPI 진입점: 헬스 체크와 /internal/v1 BE -> AI API."""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from pymongo import MongoClient

from app.api.errors import install_error_handlers
from app.api.routes import health, internal
from app.core.config import Settings, configure_logging, get_settings
from app.integrations.policy_gateway import PolicyDocumentGateway, UnconfiguredPolicyGateway
from app.integrations.spring_client import SpringInternalClient
from app.integrations.spring_policy_gateway import SpringPolicyGateway
from app.rag.embeddings import BgeM3Embedder
from app.visa.roadmap.service import StepWriter
from app.visa.roadmap.text import LlmStepWriter

logger = logging.getLogger(__name__)


@dataclass
class AppState:
    """앱 수명 동안 공유하는 상태. ``app.state.rag``에 두고 라우트가 꺼내 쓴다."""

    settings: Settings
    gateway: PolicyDocumentGateway
    embedder: BgeM3Embedder
    mongo: MongoClient[dict[str, Any]] | None = None
    # Chat tool -> Spring INT-06 client, created on first use in anthropic mode.
    chat_spring: SpringInternalClient | None = None
    errors: dict[str, str] = field(default_factory=dict)
    # "not_loaded" | "loading" | "loaded" | "error". readiness가 이 값을 보고한다.
    model_status: str = "not_loaded"
    # 로드맵 단계 문장 LLM. None이면 템플릿 문장만 쓴다.
    roadmap_writer: StepWriter | None = None


def _load_model(state: AppState) -> None:
    """작업 스레드에서 실행해 가중치를 내려받는 동안에도 서버가 헬스 체크에 답하게 한다."""
    state.model_status = "loading"
    try:
        state.embedder.load()
    except Exception as exc:  # readiness는 예외 타입만 보고한다. 다른 곳에 상세를 흘리지 않는다
        state.errors["model"] = type(exc).__name__
        state.model_status = "error"
        logger.exception("embedding model failed to load")
        return
    state.model_status = "loaded"


def _roadmap_writer(settings: Settings) -> StepWriter | None:
    if not (settings.roadmap_llm_text and settings.anthropic_configured()):
        return None
    import anthropic

    budget = settings.roadmap_llm_timeout_seconds
    # 시간 관리는 LlmStepWriter가 한다(호출마다 남은 시간).
    # SDK 재시도는 그 시간을 넘기게 하므로 끈다.
    client = anthropic.Anthropic(
        api_key=settings.anthropic_api_key.get_secret_value(),
        timeout=budget,
        max_retries=0,
    )
    return LlmStepWriter(client, settings.agent_model, timeout_seconds=budget)


def build_state(settings: Settings) -> AppState:
    embedder = BgeM3Embedder(
        settings.embedding_model,
        revision=settings.embedding_model_revision,
        device=settings.embedding_device,
        batch_size=settings.embedding_batch_size,
    )
    gateway: PolicyDocumentGateway = UnconfiguredPolicyGateway()
    if settings.spring_configured():
        # 검색은 모든 결과를 Spring(AI-R1)을 거쳐 MySQL 기준으로 확인한다. fail-closed.
        gateway = SpringPolicyGateway(
            base_url=settings.spring_internal_api_url,
            api_key=settings.spring_internal_api_key.get_secret_value(),
            header=settings.internal_token_header,
        )
    state = AppState(
        settings=settings,
        gateway=gateway,
        embedder=embedder,
        roadmap_writer=_roadmap_writer(settings),
    )
    if settings.mongo_configured():
        state.mongo = MongoClient(
            settings.mongo_uri.get_secret_value(),
            serverSelectionTimeoutMS=3000,
            tz_aware=True,
        )
    return state


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    state = build_state(settings)
    app.state.rag = state
    if settings.load_model_on_startup:
        # 첫 시작에는 수 GB를 내려받는다. 시작을 막지 않는다(그동안 live가 응답해야 함).
        threading.Thread(
            target=_load_model, args=(state,), name="model-loader", daemon=True
        ).start()
    try:
        yield
    finally:
        if state.mongo is not None:
            state.mongo.close()
        if isinstance(state.gateway, SpringPolicyGateway):
            state.gateway.close()
        if state.chat_spring is not None:
            state.chat_spring.close()


def create_app() -> FastAPI:
    app = FastAPI(title="hanstep-ai", lifespan=lifespan)
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(internal.router)
    return app


app = create_app()

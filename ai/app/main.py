"""FastAPI entry point: health checks and the /internal/v1 BE -> AI API."""

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
from app.integrations.spring_policy_gateway import SpringPolicyGateway
from app.rag.embeddings import BgeM3Embedder

logger = logging.getLogger(__name__)


@dataclass
class AppState:
    settings: Settings
    gateway: PolicyDocumentGateway
    embedder: BgeM3Embedder
    mongo: MongoClient[dict[str, Any]] | None = None
    errors: dict[str, str] = field(default_factory=dict)
    # "not_loaded" | "loading" | "loaded" | "error"; readiness reports it.
    model_status: str = "not_loaded"


def _load_model(state: AppState) -> None:
    """Runs in a worker thread so the server answers health checks while weights download."""
    state.model_status = "loading"
    try:
        state.embedder.load()
    except Exception as exc:  # readiness reports the type; never leak details elsewhere
        state.errors["model"] = type(exc).__name__
        state.model_status = "error"
        logger.exception("embedding model failed to load")
        return
    state.model_status = "loaded"


def build_state(settings: Settings) -> AppState:
    embedder = BgeM3Embedder(
        settings.embedding_model,
        revision=settings.embedding_model_revision,
        device=settings.embedding_device,
        batch_size=settings.embedding_batch_size,
    )
    gateway: PolicyDocumentGateway = UnconfiguredPolicyGateway()
    if settings.spring_configured():
        # Search verifies every result against MySQL through Spring (AI-R1); fail-closed.
        gateway = SpringPolicyGateway(
            base_url=settings.spring_internal_api_url,
            api_key=settings.spring_internal_api_key.get_secret_value(),
            header=settings.internal_token_header,
        )
    state = AppState(settings=settings, gateway=gateway, embedder=embedder)
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
        # First start downloads several GB; do not block startup (live must answer meanwhile).
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


def create_app() -> FastAPI:
    app = FastAPI(title="hanstep-ai", lifespan=lifespan)
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(internal.router)
    return app


app = create_app()

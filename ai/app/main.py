"""FastAPI entry point: health checks, internal search, ingestion job endpoints."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from pymongo import MongoClient

from app.api.routes import health, internal
from app.core.config import Settings, configure_logging, get_settings
from app.integrations.policy_gateway import PolicyDocumentGateway, UnconfiguredPolicyGateway
from app.rag.embeddings import BgeM3Embedder


@dataclass
class AppState:
    settings: Settings
    gateway: PolicyDocumentGateway
    embedder: BgeM3Embedder
    mongo: MongoClient[dict[str, Any]] | None = None
    errors: dict[str, str] = field(default_factory=dict)


def build_state(settings: Settings) -> AppState:
    embedder = BgeM3Embedder(
        settings.embedding_model,
        revision=settings.embedding_model_revision,
        device=settings.embedding_device,
        batch_size=settings.embedding_batch_size,
    )
    state = AppState(settings=settings, gateway=UnconfiguredPolicyGateway(), embedder=embedder)
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
    if settings.load_model_on_startup:
        try:
            state.embedder.load()
        except Exception as exc:  # readiness reports it; never leak details elsewhere
            state.errors["model"] = type(exc).__name__
    app.state.rag = state
    try:
        yield
    finally:
        if state.mongo is not None:
            state.mongo.close()


def create_app() -> FastAPI:
    app = FastAPI(title="hanstep-ai", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(internal.router)
    return app


app = create_app()

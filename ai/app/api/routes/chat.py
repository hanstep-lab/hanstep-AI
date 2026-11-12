"""``POST /internal/v1/agent/chat/stream`` (INT-05, Spring CHT-04 -> FastAPI, SSE).

Body validation, the X-Chat-Config override and model setup run as dependencies, so their
errors use the normal JSON envelope before the stream starts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent
from langchain_core.language_models import BaseChatModel

from app.api.errors import ApiError, request_id
from app.chat.fake_llm import FAKE_MODEL_ID, FakeChatModel
from app.chat.schemas import ChatStreamRequest, LlmConfig
from app.chat.stream import ChatRunOptions, run_chat
from app.chat.tools import (
    FinanceContextSource,
    FinanceTool,
    FixtureFinanceContext,
    SpringFinanceContext,
)
from app.core.config import Settings
from app.integrations.spring_client import SpringInternalClient

router = APIRouter(prefix="/agent/chat", tags=["chat"])


@dataclass(frozen=True)
class ChatSetup:
    config: LlmConfig
    model: BaseChatModel
    model_id: str


def build_model(settings: Settings, config: LlmConfig) -> BaseChatModel:
    if settings.chat_llm_mode == "fake":
        return FakeChatModel(delta_delay_ms=settings.chat_fake_delta_delay_ms)
    from langchain_anthropic import ChatAnthropic

    kwargs: dict[str, Any] = {
        "model": config.model,
        "max_tokens": config.max_tokens,
        "max_retries": 1,
        "api_key": settings.anthropic_api_key,
    }
    # model_default: no thinking parameter, the model's own default (adaptive on 5.5 models).
    if config.thinking != "model_default":
        kwargs["thinking"] = {"type": config.thinking}
    if config.effort:
        # Sent as output_config.effort by langchain-anthropic.
        kwargs["reasoning_effort"] = config.effort
    return ChatAnthropic(**kwargs)


def chat_setup(
    request: Request, x_chat_config: Annotated[str | None, Header()] = None
) -> ChatSetup:
    settings: Settings = request.app.state.rag.settings
    config = LlmConfig.from_settings(settings)
    # Ignored unless explicitly allowed (measurement sessions only).
    if x_chat_config and settings.chat_allow_config_override:
        try:
            config = config.with_override(x_chat_config)
        except ValueError as exc:
            raise ApiError(400, "VALIDATION_ERROR", str(exc)) from exc
    if settings.chat_llm_mode == "anthropic" and not settings.anthropic_configured():
        raise ApiError(503, "SERVICE_UNAVAILABLE", "chat model is not configured")
    model_id = FAKE_MODEL_ID if settings.chat_llm_mode == "fake" else config.model
    return ChatSetup(config=config, model=build_model(settings, config), model_id=model_id)


def finance_source(request: Request) -> FinanceContextSource:
    state = request.app.state.rag
    settings: Settings = state.settings
    if settings.chat_llm_mode == "anthropic" and settings.spring_configured():
        if state.chat_spring is None:
            state.chat_spring = SpringInternalClient(
                base_url=settings.spring_internal_api_url,
                api_key=settings.spring_internal_api_key.get_secret_value(),
                header=settings.internal_token_header,
            )
        return SpringFinanceContext(state.chat_spring, settings.chat_tool_timeout_seconds)
    return FixtureFinanceContext()


@router.post("/stream", response_class=EventSourceResponse)
async def chat_stream(
    body: ChatStreamRequest,
    request: Request,
    setup: Annotated[ChatSetup, Depends(chat_setup)],
    source: Annotated[FinanceContextSource, Depends(finance_source)],
    x_chat_metrics: Annotated[str | None, Header()] = None,
) -> AsyncIterator[ServerSentEvent]:
    settings: Settings = request.app.state.rag.settings
    tool = FinanceTool(
        source,
        user_id=body.user_id,
        request_id=request_id(request),
        timeout=settings.chat_tool_timeout_seconds,
        message=body.message,
        judgment_facts=setup.config.judgment_facts,
    )
    options = ChatRunOptions(
        max_tool_calls=settings.chat_max_tool_calls,
        run_timeout_seconds=settings.chat_run_timeout_seconds,
        metrics=x_chat_metrics == "1",
        model_id=setup.model_id,
    )
    async for event in run_chat(
        body, config=setup.config, model=setup.model, tool=tool, options=options
    ):
        yield event

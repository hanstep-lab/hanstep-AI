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
    ChatToolset,
    FinanceContextSource,
    FinanceTool,
    FixtureFinanceContext,
    SpringFinanceContext,
)
from app.chat.visa_tools import (
    E74AssessmentSource,
    PolicySearchSource,
    ServicePolicySearch,
    SpringE74Source,
    VisaTools,
)
from app.core.config import Settings
from app.integrations.spring_client import SpringInternalClient
from app.rag.retrieval import SearchService

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


def _chat_spring(request: Request) -> SpringInternalClient:
    state = request.app.state.rag
    settings: Settings = state.settings
    if state.chat_spring is None:
        state.chat_spring = SpringInternalClient(
            base_url=settings.spring_internal_api_url,
            api_key=settings.spring_internal_api_key.get_secret_value(),
            header=settings.internal_token_header,
        )
    client: SpringInternalClient = state.chat_spring
    return client


def finance_source(request: Request) -> FinanceContextSource:
    settings: Settings = request.app.state.rag.settings
    if settings.chat_llm_mode == "anthropic" and settings.spring_configured():
        return SpringFinanceContext(_chat_spring(request), settings.chat_tool_timeout_seconds)
    return FixtureFinanceContext()


def e74_source(request: Request) -> E74AssessmentSource | None:
    """E-7-4 점수는 Spring이 MySQL 프로필로 계산·저장한다. 설정이 없으면 None(도구가 FAILED)."""
    settings: Settings = request.app.state.rag.settings
    if settings.chat_llm_mode == "anthropic" and settings.spring_configured():
        return SpringE74Source(_chat_spring(request), settings.chat_tool_timeout_seconds)
    return None


def policy_search(request: Request) -> PolicySearchSource | None:
    """정책 검색 라우트와 같은 운영 검색. 벡터 저장소·임베딩 모델이 없으면 None(도구가 FAILED)."""
    state = request.app.state.rag
    settings: Settings = state.settings
    if state.mongo is None or not state.embedder.loaded:
        return None
    service = SearchService(
        state.mongo[settings.mongo_database][settings.mongo_collection],
        state.embedder,
        state.gateway,
        index_name=settings.mongo_vector_index,
        num_candidates=settings.search_num_candidates,
    )
    return ServicePolicySearch(service)


@router.post("/stream", response_class=EventSourceResponse)
async def chat_stream(
    body: ChatStreamRequest,
    request: Request,
    setup: Annotated[ChatSetup, Depends(chat_setup)],
    source: Annotated[FinanceContextSource, Depends(finance_source)],
    e74: Annotated[E74AssessmentSource | None, Depends(e74_source)],
    search: Annotated[PolicySearchSource | None, Depends(policy_search)],
    x_chat_metrics: Annotated[str | None, Header()] = None,
) -> AsyncIterator[ServerSentEvent]:
    settings: Settings = request.app.state.rag.settings
    rid = request_id(request)
    # 사용자 id·요청 id는 요청마다 고정한다(LLM 인자로 바꿀 수 없음).
    tool = ChatToolset(
        FinanceTool(
            source,
            user_id=body.user_id,
            request_id=rid,
            timeout=settings.chat_tool_timeout_seconds,
        ),
        VisaTools(
            e74_source=e74,
            search_source=search,
            user_id=body.user_id,
            request_id=rid,
            timeout=settings.chat_tool_timeout_seconds,
        ),
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

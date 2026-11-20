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
from app.chat.finance_policy_tools import FinancePolicyTool
from app.chat.fx_tools import FixtureFxSummary, FxSummarySource, FxTools, SpringFxSummary
from app.chat.product_tools import (
    TOOL_SCHEMAS_BY_PROMPT,
    FixtureProductContext,
    ProductContextSource,
    ProductTool,
    SpringProductContext,
)
from app.chat.remittance_tools import (
    FixtureRemittanceSource,
    RemittanceSource,
    RemittanceTools,
    SpringRemittanceSource,
)
from app.chat.schemas import ChatStreamRequest, LlmConfig
from app.chat.stream import ChatRunOptions, run_chat
from app.chat.tools import (
    ChatTool,
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
from app.finance_policy.labels import default_labels
from app.integrations.spring_client import SpringInternalClient
from app.rag.retrieval import SearchService

router = APIRouter(prefix="/agent/chat", tags=["chat"])

# 비자 도구(정책 검색·E-7-4 점수)를 설명하는 프롬프트 버전. 이 버전에서만 비자 도구를 붙인다.
# v1·v1.1은 금융 도구만 붙어서 그 버전으로 잰 측정 조건이 그대로 남는다.
VISA_PROMPT_VERSIONS = frozenset(
    {"v2", "v3", "v3.1", "v3.2", "v4", "v4.1", "v4.2", "v4.3a", "v4.4", "v4.5"}
)
# 송금 조회 도구(받는 분·환율·견적)를 설명하는 프롬프트 버전. v1~v2의 도구 구성은 그대로다.
REMITTANCE_PROMPT_VERSIONS = frozenset(
    {"v3", "v3.1", "v3.2", "v4", "v4.1", "v4.2", "v4.3a", "v4.4", "v4.5"}
)
# 상품 추천 도구(#65)를 설명하는 프롬프트 버전. v3까지의 도구 구성은 그대로다.
PRODUCT_PROMPT_VERSIONS = frozenset({"v4", "v4.1", "v4.2", "v4.3a", "v4.4", "v4.5"})
# 금융 정책 검색 도구(#88)를 설명하는 프롬프트 버전.
# v4.1까지의 도구 구성은 그대로다(기준선 영향 없음).
FINANCE_POLICY_PROMPT_VERSIONS = frozenset({"v4.2", "v4.3a", "v4.4", "v4.5"})
# 환율 진단 도구(fx_rate_summary, #91)를 설명하는 프롬프트 버전. v4.4 = v4.2 + 환율 진단 규칙이라
# v4.4에는 v4.2의 도구가 모두 붙고 이 도구가 더 붙는다. 다른 버전의 도구 구성은 그대로다.
FX_PROMPT_VERSIONS = frozenset({"v4.4", "v4.5"})
# 금융상품 추천 카드(PRODUCT_RECOMMEND_CARD, #114)를 설명하는 프롬프트 버전.
# v4.5 = v4.4 + 목표·출처·카드 규칙이라 도구는 v4.4와 같고 상품 도구가 카드를 더 낸다.
# 다른 버전에는 카드가 없다.
PRODUCT_CARD_PROMPT_VERSIONS = frozenset({"v4.5"})


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


def remittance_source(request: Request) -> RemittanceSource:
    """송금 받는 분·환율·견적. fake 모드이거나 Spring 설정이 없으면 데모 fixture."""
    settings: Settings = request.app.state.rag.settings
    if settings.chat_llm_mode == "anthropic" and settings.spring_configured():
        return SpringRemittanceSource(_chat_spring(request), settings.chat_tool_timeout_seconds)
    return FixtureRemittanceSource()


def fx_source(request: Request) -> FxSummarySource:
    """환율 진단(fx-summary). fake 모드이거나 Spring 설정이 없으면 Reyes 실제 출력 fixture."""
    settings: Settings = request.app.state.rag.settings
    if settings.chat_llm_mode == "anthropic" and settings.spring_configured():
        return SpringFxSummary(_chat_spring(request), settings.chat_tool_timeout_seconds)
    return FixtureFxSummary()


def product_source(
    request: Request, x_chat_product_profile: Annotated[str | None, Header()] = None
) -> ProductContextSource:
    """상품 추천 프로필. fake 모드이거나 Spring 설정이 없으면 fixture 프로필(기본 P1).

    측정 전용: CHAT_ALLOW_CONFIG_OVERRIDE=true일 때만 X-Chat-Product-Profile을 쓴다. 값은 fixture
    id(P1~P5) 또는 프로필 값 JSON(visaTypeCode·stayExpiresOn·age·asOf·spareKrw, evaluate_chat이
    질문 파일의 profiles로 만든다). 운영 경로(anthropic 모드 + Spring 설정)에서는 쓰지 않는다.
    """
    settings: Settings = request.app.state.rag.settings
    if settings.chat_llm_mode == "anthropic" and settings.spring_configured():
        return SpringProductContext(_chat_spring(request), settings.chat_tool_timeout_seconds)
    profile = x_chat_product_profile if settings.chat_allow_config_override else None
    try:
        return FixtureProductContext(profile)
    except ValueError as exc:
        raise ApiError(400, "VALIDATION_ERROR", str(exc)) from exc


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
    remittance: Annotated[RemittanceSource, Depends(remittance_source)],
    product: Annotated[ProductContextSource, Depends(product_source)],
    fx: Annotated[FxSummarySource, Depends(fx_source)],
    x_chat_metrics: Annotated[str | None, Header()] = None,
) -> AsyncIterator[ServerSentEvent]:
    settings: Settings = request.app.state.rag.settings
    rid = request_id(request)
    # 사용자 id·요청 id는 요청마다 고정한다(LLM 인자로 바꿀 수 없음).
    tools: list[ChatTool] = [
        FinanceTool(
            source,
            user_id=body.user_id,
            request_id=rid,
            timeout=settings.chat_tool_timeout_seconds,
            message=body.message,
            judgment_facts=setup.config.judgment_facts,
        )
    ]
    # 비자 도구는 그 도구를 설명하는 프롬프트 버전에서만 붙인다.
    if setup.config.prompt_version in VISA_PROMPT_VERSIONS:
        tools.append(
            VisaTools(
                e74_source=e74,
                search_source=search,
                user_id=body.user_id,
                request_id=rid,
                timeout=settings.chat_tool_timeout_seconds,
            )
        )
    # 송금 조회 도구도 그 도구를 설명하는 프롬프트 버전(v3)에서만 붙인다. 송금은 실행하지 않는다.
    if setup.config.prompt_version in REMITTANCE_PROMPT_VERSIONS:
        tools.append(
            RemittanceTools(
                remittance,
                user_id=body.user_id,
                request_id=rid,
                timeout=settings.chat_tool_timeout_seconds,
            )
        )
    # 상품 추천 도구는 v4·v4.1에서만 붙는다. 순서 B(검색)는 상품설명서 검색이 준비됐을 때만.
    if setup.config.prompt_version in PRODUCT_PROMPT_VERSIONS:
        tools.append(
            ProductTool(
                product,
                user_id=body.user_id,
                request_id=rid,
                timeout=settings.chat_tool_timeout_seconds,
                ranker=request.app.state.rag.product_search,
                schema=TOOL_SCHEMAS_BY_PROMPT[setup.config.prompt_version],
                card=setup.config.prompt_version in PRODUCT_CARD_PROMPT_VERSIONS,
            )
        )
    # 금융 정책 검색 도구는 v4.2·v4.3a·v4.4에서만 붙는다. sources는 비운다(본문에 조문·시행일 인용).
    if setup.config.prompt_version in FINANCE_POLICY_PROMPT_VERSIONS:
        tools.append(
            FinancePolicyTool(
                request.app.state.rag.finance_policy_search,
                timeout=settings.chat_tool_timeout_seconds,
                labels=default_labels() if settings.finance_policy_labels else None,
            )
        )
    # 환율 진단 도구는 FX_PROMPT_VERSIONS에서만 붙는다.
    # 숫자는 Spring이 계산하고, 과거 평균 비교만 준다.
    if setup.config.prompt_version in FX_PROMPT_VERSIONS:
        tools.append(
            FxTools(
                fx,
                user_id=body.user_id,
                request_id=rid,
                timeout=settings.chat_tool_timeout_seconds,
            )
        )
    tool = ChatToolset(*tools)
    options = ChatRunOptions(
        max_tool_calls=settings.chat_max_tool_calls,
        run_timeout_seconds=settings.chat_run_timeout_seconds,
        metrics=x_chat_metrics == "1",
        model_id=setup.model_id,
        answer_guard=settings.finance_policy_answer_guard,
    )
    async for event in run_chat(
        body, config=setup.config, model=setup.model, tool=tool, options=options
    ):
        yield event

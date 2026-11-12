"""``ai/.env``와 프로세스 환경 변수에서 읽는 애플리케이션 설정."""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AI_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = AI_ROOT.parent

MIN_INTERNAL_TOKEN_LENGTH = 32
PLACEHOLDER_MARKERS = ("your-", "CHANGE_ME", "example.", "username:password")

ChatThinking = Literal["model_default", "adaptive", "between_tools", "disabled"]
ChatEffort = Literal["", "low", "medium", "high"]
# Efforts allowed when thinking is turned off (between_tools / disabled).
_EFFORTS_WITHOUT_THINKING = {"", "low", "medium", "high"}


def check_chat_config(model: str, thinking: str, effort: str) -> None:
    """Reject thinking/effort combinations the model does not accept.

    Rules are known for the two 5.5 models only; other models are passed through unchecked.
    Sonnet 5.5: no ``disabled``; ``between_tools`` with effort high or below.
    Haiku 5.5: no ``between_tools``; ``disabled`` with effort high or below.
    """
    if model.startswith("claude-sonnet-5-5"):
        if thinking == "disabled":
            raise ValueError(f"{model} does not accept CHAT_THINKING=disabled")
    elif model.startswith("claude-haiku-5-5"):
        if thinking == "between_tools":
            raise ValueError(f"{model} does not accept CHAT_THINKING=between_tools")
    else:
        return
    if thinking in ("between_tools", "disabled") and effort not in _EFFORTS_WITHOUT_THINKING:
        raise ValueError(f"{model} with CHAT_THINKING={thinking} needs CHAT_EFFORT high or below")


def is_placeholder(value: str | None) -> bool:
    """값이 비었거나 아직 ``.env.example``의 예시값이면 True."""
    if not value:
        return True
    return any(marker in value for marker in PLACEHOLDER_MARKERS)


class Settings(BaseSettings):
    """AI 서버 설정. 외부 연동별로 설정을 마쳤는지도 판정한다."""

    model_config = SettingsConfigDict(
        env_file=AI_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_env: str = "local"
    log_level: str = "INFO"

    mongo_uri: SecretStr = SecretStr("")
    mongo_database: str = "shinhan_rag"
    mongo_collection: str = "visa_policy_chunks"
    mongo_vector_index: str = "rag_vector_index"

    embedding_model: str = "BAAI/bge-m3"
    # 재현 가능한 실행을 위해 Hugging Face 커밋을 고정한다. 비우면 hub 기본 브랜치를 쓴다.
    embedding_model_revision: str = ""
    embedding_device: str = "cpu"
    embedding_batch_size: int = Field(default=8, ge=1, le=256)
    load_model_on_startup: bool = False

    # AI -> Spring 발신 호출.
    spring_internal_api_url: str = ""
    spring_internal_api_key: SecretStr = SecretStr("")
    # /internal/v1 아래의 BE -> AI 수신 호출. 비밀값 하나가 유출돼도 양방향이 다 열리지
    # 않도록 발신 키와 따로 둔다.
    ai_internal_api_token: SecretStr = SecretStr("")
    internal_token_header: str = "X-Internal-Token"

    r2_endpoint_url: str = ""
    r2_access_key_id: SecretStr = SecretStr("")
    r2_secret_access_key: SecretStr = SecretStr("")
    r2_region: str = "auto"
    # 비자 정책 원본 / 금융상품 설명서·약관 / ARC 이미지(비공개).
    r2_policy_bucket: str = ""
    r2_product_bucket: str = ""
    r2_arc_bucket: str = ""
    # 제안값일 뿐이다. 운영에 쓰기 전에 객체 키 계약을 확정한다.
    r2_policy_prefix: str = "policy-sources"

    anthropic_api_key: SecretStr = SecretStr("")
    agent_model: str = "claude-haiku-5-5"

    # E-7-4 계산에 승인 전(DRAFT) 규칙 버전도 쓸지. 운영은 false, 로컬·시연에서만 true.
    e74_include_draft_rules: bool = False
    # 로드맵 단계 문장을 LLM(agent_model)으로 쓸지. 꺼져 있거나 키가 없으면 템플릿 문장.
    roadmap_llm_text: bool = False
    # 문장 생성 전체(재시도 포함)의 시간. 넘기면 템플릿 문장. Spring 호출 타임아웃보다 짧게 둔다.
    roadmap_llm_timeout_seconds: float = Field(default=15.0, gt=0, le=60)

    # Chat agent (INT-05). fake runs the same graph without an API key.
    chat_llm_mode: Literal["fake", "anthropic"] = "fake"
    # model_default sends no thinking parameter (both 5.5 models then think adaptively).
    chat_thinking: ChatThinking = "model_default"
    chat_effort: ChatEffort = ""
    # Thinking tokens count against max_tokens, so leave room.
    chat_max_tokens: int = Field(default=2000, ge=1, le=64000)
    chat_max_tool_calls: int = Field(default=3, ge=0, le=10)
    chat_tool_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    chat_run_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    chat_fake_delta_delay_ms: int = Field(default=30, ge=0, le=5000)
    # Add code-computed judgment facts (judgmentFacts) to the finance tool result.
    chat_judgment_facts: bool = False
    # Measurement only: honour the per-request X-Chat-Config header.
    chat_allow_config_override: bool = False

    law_api_base_url: str = "https://www.law.go.kr"
    law_api_oc: SecretStr = SecretStr("")

    rag_data_root: Path = REPO_ROOT / "data" / "rag_documents"
    rag_artifacts_dir: Path = AI_ROOT / "artifacts" / "rag"
    search_top_k_max: int = 20
    search_num_candidates: int = 100
    search_query_max_chars: int = 1000

    def mongo_configured(self) -> bool:
        return not is_placeholder(self.mongo_uri.get_secret_value())

    def r2_credentials_configured(self) -> bool:
        values = (
            self.r2_endpoint_url,
            self.r2_access_key_id.get_secret_value(),
            self.r2_secret_access_key.get_secret_value(),
        )
        return not any(is_placeholder(value) for value in values)

    def r2_configured(self) -> bool:
        return self.r2_credentials_configured() and not is_placeholder(self.r2_policy_bucket)

    def r2_bucket_for(self, domain: str) -> str:
        """RAG 도메인별 원본 버킷. ARC 버킷은 절대 RAG 원본이 되지 않는다."""
        bucket = {"visa": self.r2_policy_bucket, "finance": self.r2_product_bucket}.get(domain, "")
        if is_placeholder(bucket):
            return ""
        if bucket == self.r2_arc_bucket:
            raise ValueError(f"{domain} bucket must not be the ARC bucket")
        return bucket

    @field_validator("embedding_model_revision")
    @classmethod
    def _revision_is_commit_or_branch(cls, value: str) -> str:
        # 여기에 비밀번호를 잘못 붙여 넣거나 오타가 나면 알 수 없는 모델 revision을 조용히
        # 받아 온다.
        if value and not re.fullmatch(r"[0-9a-f]{7,40}|main", value):
            raise ValueError("EMBEDDING_MODEL_REVISION must be a commit hash, 'main' or empty")
        return value

    @field_validator("r2_endpoint_url")
    @classmethod
    def _endpoint_without_path(cls, value: str) -> str:
        # 대시보드는 버킷별 URL(".../<bucket>")을 보여 주지만 boto3에는 계정 엔드포인트가
        # 필요하다.
        parsed = urlparse(value)
        if value and parsed.path not in ("", "/"):
            raise ValueError(
                "R2_ENDPOINT_URL must be https://<account_id>.r2.cloudflarestorage.com "
                "without a bucket name or path"
            )
        return value.rstrip("/")

    @model_validator(mode="after")
    def _chat_config_allowed(self) -> Settings:
        # Fail at startup, not on the first chat request.
        check_chat_config(self.agent_model, self.chat_thinking, self.chat_effort)
        return self

    @model_validator(mode="after")
    def _distinct_buckets(self) -> Settings:
        named = [
            b for b in (self.r2_policy_bucket, self.r2_product_bucket, self.r2_arc_bucket) if b
        ]
        if len(named) != len(set(named)):
            raise ValueError("R2 policy, product and ARC buckets must be different")
        return self

    def internal_token_configured(self) -> bool:
        token = self.ai_internal_api_token.get_secret_value()
        return not is_placeholder(token) and len(token) >= MIN_INTERNAL_TOKEN_LENGTH

    def anthropic_configured(self) -> bool:
        return not is_placeholder(self.anthropic_api_key.get_secret_value())

    def spring_configured(self) -> bool:
        return not (
            is_placeholder(self.spring_internal_api_url)
            or is_placeholder(self.spring_internal_api_key.get_secret_value())
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx는 INFO에서 요청 URL 전체를 남기므로 쿼리 문자열의 인증값(OC)이 새어 나간다.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

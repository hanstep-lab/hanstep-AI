"""Application settings loaded from ``ai/.env`` and the process environment."""

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
    """True when a value is empty or still the ``.env.example`` placeholder."""
    if not value:
        return True
    return any(marker in value for marker in PLACEHOLDER_MARKERS)


class Settings(BaseSettings):
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
    # Pin a Hugging Face commit for reproducible runs; empty means the hub default branch.
    embedding_model_revision: str = ""
    embedding_device: str = "cpu"
    embedding_batch_size: int = Field(default=8, ge=1, le=256)
    load_model_on_startup: bool = False

    # Outbound AI -> Spring calls.
    spring_internal_api_url: str = ""
    spring_internal_api_key: SecretStr = SecretStr("")
    # Inbound BE -> AI calls under /internal/v1. Kept separate from the outbound key so one
    # leaked secret does not open both directions.
    ai_internal_api_token: SecretStr = SecretStr("")
    internal_token_header: str = "X-Internal-Token"

    r2_endpoint_url: str = ""
    r2_access_key_id: SecretStr = SecretStr("")
    r2_secret_access_key: SecretStr = SecretStr("")
    r2_region: str = "auto"
    # Visa policy originals / financial product descriptions & terms / ARC images (private).
    r2_policy_bucket: str = ""
    r2_product_bucket: str = ""
    r2_arc_bucket: str = ""
    # Proposal only; confirm the object key contract before production use.
    r2_policy_prefix: str = "policy-sources"

    anthropic_api_key: SecretStr = SecretStr("")
    agent_model: str = "claude-sonnet-5-5"

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
        """Source bucket per RAG domain. The ARC bucket is never a RAG source."""
        bucket = {"visa": self.r2_policy_bucket, "finance": self.r2_product_bucket}.get(domain, "")
        if is_placeholder(bucket):
            return ""
        if bucket == self.r2_arc_bucket:
            raise ValueError(f"{domain} bucket must not be the ARC bucket")
        return bucket

    @field_validator("embedding_model_revision")
    @classmethod
    def _revision_is_commit_or_branch(cls, value: str) -> str:
        # A pasted password or typo here would silently pull an unknown model revision.
        if value and not re.fullmatch(r"[0-9a-f]{7,40}|main", value):
            raise ValueError("EMBEDDING_MODEL_REVISION must be a commit hash, 'main' or empty")
        return value

    @field_validator("r2_endpoint_url")
    @classmethod
    def _endpoint_without_path(cls, value: str) -> str:
        # The dashboard shows per-bucket URLs (".../<bucket>"); boto3 needs the account endpoint.
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
    # httpx logs full request URLs at INFO, which would leak query-string credentials (OC).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

"""Application settings loaded from ``ai/.env`` and the process environment."""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

AI_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = AI_ROOT.parent

MIN_INTERNAL_TOKEN_LENGTH = 32
PLACEHOLDER_MARKERS = ("your-", "CHANGE_ME", "example.", "username:password")


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
    r2_policy_bucket: str = ""
    r2_arc_bucket: str = ""
    # Proposal only; confirm the object key contract before production use.
    r2_policy_prefix: str = "policy-sources"

    law_api_base_url: str = "https://www.law.go.kr"
    law_api_oc: SecretStr = SecretStr("")

    rag_data_root: Path = REPO_ROOT / "data" / "rag_documents"
    rag_artifacts_dir: Path = AI_ROOT / "artifacts" / "rag"
    search_top_k_max: int = 20
    search_num_candidates: int = 100
    search_query_max_chars: int = 1000

    def mongo_configured(self) -> bool:
        return not is_placeholder(self.mongo_uri.get_secret_value())

    def r2_configured(self) -> bool:
        values = (
            self.r2_endpoint_url,
            self.r2_access_key_id.get_secret_value(),
            self.r2_secret_access_key.get_secret_value(),
            self.r2_policy_bucket,
        )
        return not any(is_placeholder(value) for value in values)

    def internal_token_configured(self) -> bool:
        token = self.ai_internal_api_token.get_secret_value()
        return not is_placeholder(token) and len(token) >= MIN_INTERNAL_TOKEN_LENGTH

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

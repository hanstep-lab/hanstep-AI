"""Worker 설정. 프로세스 환경 변수에서만 읽는다(``ocr/.env``는 두지 않는다).

compose는 루트 ``.env``에서 필요한 키만 ``environment``로 넘긴다. R2 키(R2_OCR_*)에는 루트
``.env``의 ARC 버킷 읽기 전용 토큰(R2_OCR_READ_*)이 들어온다.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Annotated
from urllib.parse import urlparse

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PLACEHOLDER_MARKERS = ("your-", "CHANGE_ME", "example.", "username:password")


def is_placeholder(value: str | None) -> bool:
    """값이 비었거나 아직 ``.env.example``의 예시값이면 True."""
    if not value:
        return True
    return any(marker in value for marker in PLACEHOLDER_MARKERS)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    log_level: str = "INFO"

    rabbitmq_url: SecretStr = SecretStr("")
    # 일시 오류 재시도 대기(ms). n번째 재시도 메시지가 n번째 값을 expiration으로 쓰고, 값 개수가
    # 최대 재시도 횟수다. 환경 변수는 "5000,15000,45000".
    retry_delays_ms: Annotated[tuple[int, ...], NoDecode] = (5000, 15000, 45000)
    # job_started의 workerId. 비우면 호스트 이름을 쓴다.
    worker_id: str = ""

    spring_internal_api_url: str = ""
    spring_internal_api_key: SecretStr = SecretStr("")
    internal_token_header: str = "X-Internal-Token"

    r2_endpoint_url: str = ""
    r2_region: str = "auto"
    r2_ocr_access_key_id: SecretStr = SecretStr("")
    r2_ocr_secret_access_key: SecretStr = SecretStr("")
    r2_arc_bucket: str = ""

    @field_validator("retry_delays_ms", mode="before")
    @classmethod
    def _parse_retry_delays(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(int(part) for part in value.split(",") if part.strip())
        return value

    @field_validator("retry_delays_ms")
    @classmethod
    def _retry_delays_positive(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value or any(delay <= 0 for delay in value):
            raise ValueError(
                "RETRY_DELAYS_MS must list positive milliseconds, e.g. 5000,15000,45000"
            )
        return value

    @field_validator("r2_endpoint_url")
    @classmethod
    def _endpoint_without_path(cls, value: str) -> str:
        # 대시보드는 버킷별 URL(".../<bucket>")을 보여 주지만 boto3에는 계정 엔드포인트가 필요하다.
        parsed = urlparse(value)
        if value and parsed.path not in ("", "/"):
            raise ValueError(
                "R2_ENDPOINT_URL must be https://<account_id>.r2.cloudflarestorage.com "
                "without a bucket name or path"
            )
        return value.rstrip("/")

    def missing(self) -> list[str]:
        """비었거나 예시값인 필수 설정 이름."""
        missing = []
        if is_placeholder(self.rabbitmq_url.get_secret_value()):
            missing.append("RABBITMQ_URL")
        if is_placeholder(self.spring_internal_api_url) or is_placeholder(
            self.spring_internal_api_key.get_secret_value()
        ):
            missing.append("SPRING_INTERNAL_API_URL/SPRING_INTERNAL_API_KEY")
        r2_values = (
            self.r2_endpoint_url,
            self.r2_ocr_access_key_id.get_secret_value(),
            self.r2_ocr_secret_access_key.get_secret_value(),
            self.r2_arc_bucket,
        )
        if any(is_placeholder(value) for value in r2_values):
            missing.append(
                "R2_ENDPOINT_URL/R2_OCR_ACCESS_KEY_ID/R2_OCR_SECRET_ACCESS_KEY/R2_ARC_BUCKET"
            )
        return missing


@lru_cache
def get_settings() -> Settings:
    return Settings()


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx는 INFO에서 요청 URL을 남긴다. botocore는 자격 증명 탐색을 INFO로 남긴다.
    for name in ("httpx", "httpcore", "botocore", "boto3"):
        logging.getLogger(name).setLevel(logging.WARNING)

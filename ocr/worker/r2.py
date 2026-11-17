"""등록증 원본을 읽는 R2(S3 호환) 저장소. ARC 버킷 읽기 전용 키(R2_OCR_*)로 GET만 한다."""

from __future__ import annotations

import hashlib
from typing import Any, BinaryIO, Protocol

# hanstep-backend DocumentService.MAX_BYTES(업로드 제한)와 같은 값.
MAX_IMAGE_BYTES = 10 * 1024 * 1024
_CHUNK_BYTES = 1024 * 1024


class ImageTooLarge(Exception):
    """저장된 원본이 업로드 제한보다 크다. 재시도해도 같다."""


class ArcImageStore(Protocol):
    def download(self, key: str, out: BinaryIO) -> str:
        """원본을 ``out``에 쓰고 SHA-256(소문자 hex)을 돌려준다.

        객체가 없으면 LookupError, 연결 실패·시간 초과·5xx는 ConnectionError,
        크기 제한을 넘으면 ImageTooLarge.
        """
        ...


class R2ArcImageStore:
    def __init__(
        self,
        *,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        region: str = "auto",
        client: Any = None,
    ) -> None:
        if not bucket:
            raise ValueError("bucket is required")
        self.bucket = bucket
        if client is not None:
            self._client = client
            return
        import boto3
        from botocore.config import Config

        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region,
            config=Config(
                connect_timeout=5,
                read_timeout=30,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    def download(self, key: str, out: BinaryIO) -> str:
        from botocore.exceptions import BotoCoreError, ClientError

        digest = hashlib.sha256()
        total = 0
        try:
            body = self._client.get_object(Bucket=self.bucket, Key=key)["Body"]
            for part in iter(lambda: body.read(_CHUNK_BYTES), b""):
                total += len(part)
                if total > MAX_IMAGE_BYTES:
                    raise ImageTooLarge(f"stored object exceeds {MAX_IMAGE_BYTES} bytes")
                digest.update(part)
                out.write(part)
        except ClientError as exc:
            # 오류 메시지에 키를 싣지 않는다(키에는 개인정보가 없지만 로그로 퍼뜨릴 이유도 없다).
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                raise LookupError("R2 object not found") from exc
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
            if status >= 500:
                raise ConnectionError(f"R2 responded {status}") from exc
            raise
        except BotoCoreError as exc:
            # 엔드포인트 연결 실패, 연결·읽기 시간 초과(boto3 재시도 3회 뒤)
            raise ConnectionError(f"R2 unavailable: {type(exc).__name__}") from exc
        return digest.hexdigest()

"""공식 정책 원본을 보관하는 비공개 R2 저장소(S3 호환, boto3).

객체는 내용 주소 방식이라 다른 원본이 기존 객체를 덮어쓸 수 없다.
ARC(외국인등록증) 버킷은 여기서 거부한다. 이 파이프라인은 그 버킷을 읽지도 쓰지도 않는다.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.config import Settings
from app.rag.errors import ContractBlockedError, ValidationFailedError

MAX_OBJECT_BYTES = 50 * 1024 * 1024
PRESIGN_MAX_SECONDS = 900

_EXT_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".hwp": "application/x-hwp",
    ".hwpx": "application/hwp+zip",
    ".xml": "application/xml",
    ".json": "application/json",
}


def object_key(prefix: str, domain: str, sha256: str, extension: str) -> str:
    """``{prefix}/{domain}/sha256/{hex}{ext}`` — 백엔드 키 계약 확정 전까지의 제안안."""
    if len(sha256) != 64:
        raise ValueError("sha256 must be 64 hex characters")
    ext = extension.lower() if extension.startswith(".") else f".{extension.lower()}"
    return f"{prefix.strip('/')}/{domain}/sha256/{sha256}{ext}"


class ObjectStore(Protocol):
    """원본 객체 저장소 인터페이스. SHA-256으로 업로드 내용과 저장 결과를 확인한다."""

    def put_if_absent(self, key: str, data: bytes, sha256: str) -> str:
        """결과로 "uploaded" 또는 "exists"를 돌려준다. 다른 내용의 객체가 있으면 예외를 던진다."""
        ...

    def read_sha256(self, key: str) -> str: ...


class UnconfiguredObjectStore:
    """R2 설정이 없을 때 쓰는 저장소. 사용하는 순간 ContractBlockedError를 낸다."""

    def __init__(self, bucket_setting: str = "R2_POLICY_BUCKET") -> None:
        self.bucket_setting = bucket_setting

    def put_if_absent(self, key: str, data: bytes, sha256: str) -> str:
        raise ContractBlockedError(
            "R2 not configured: set R2_ENDPOINT_URL, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, "
            f"{self.bucket_setting} and confirm bucket access and object key contract"
        )

    def read_sha256(self, key: str) -> str:
        raise ContractBlockedError("R2 not configured")


BUCKET_SETTINGS = {"visa": "R2_POLICY_BUCKET", "finance": "R2_PRODUCT_BUCKET"}


def build_object_stores(settings: Settings) -> dict[str, ObjectStore]:
    """RAG 도메인마다 저장소 하나: visa -> 정책 버킷, finance -> 상품 버킷.

    인증 정보나 버킷 이름이 없으면 사용 시점에 막는 UnconfiguredObjectStore를 넣는다.
    """
    stores: dict[str, ObjectStore] = {}
    for domain, setting in BUCKET_SETTINGS.items():
        bucket = settings.r2_bucket_for(domain)
        if not (settings.r2_credentials_configured() and bucket):
            stores[domain] = UnconfiguredObjectStore(setting)
            continue
        stores[domain] = R2ObjectStore(
            endpoint_url=settings.r2_endpoint_url,
            access_key_id=settings.r2_access_key_id.get_secret_value(),
            secret_access_key=settings.r2_secret_access_key.get_secret_value(),
            bucket=bucket,
            region=settings.r2_region,
            forbidden_buckets=(settings.r2_arc_bucket,) if settings.r2_arc_bucket else (),
        )
    return stores


class R2ObjectStore:
    """R2(S3 호환) 버킷 저장소. 크기와 해시를 확인하고 키가 없을 때만 올린다."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        region: str = "auto",
        forbidden_buckets: tuple[str, ...] = (),
    ) -> None:
        if not bucket:
            raise ValueError("bucket is required")
        if bucket in forbidden_buckets:
            raise ValueError("the ARC bucket must never be used by the policy pipeline")
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self._client: Any = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region,
            config=Config(
                connect_timeout=10,
                read_timeout=60,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    def _head(self, key: str) -> dict[str, Any] | None:
        from botocore.exceptions import ClientError

        try:
            head: dict[str, Any] = self._client.head_object(Bucket=self.bucket, Key=key)
            return head
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    def put_if_absent(self, key: str, data: bytes, sha256: str) -> str:
        if len(data) > MAX_OBJECT_BYTES:
            raise ValidationFailedError("object exceeds size limit")
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ValidationFailedError("data does not match declared sha256")
        head = self._head(key)
        if head is not None:
            stored = head.get("Metadata", {}).get("sha256")
            if stored != sha256:
                raise ValidationFailedError(f"R2 object {key} exists with different content")
            return "exists"
        extension = "." + key.rsplit(".", 1)[-1] if "." in key else ""
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=_EXT_CONTENT_TYPES.get(extension, "application/octet-stream"),
            # ContentMD5로 서버가 손상된 업로드를 거부하게 한다. ETag는 SHA-256으로 쓰지 않는다.
            ContentMD5=base64.b64encode(hashlib.md5(data).digest()).decode(),  # noqa: S324
            Metadata={"sha256": sha256},
        )
        return "uploaded"

    def read_sha256(self, key: str) -> str:
        """저장된 바이트를 내려받아 해시한다(왕복 검증)."""
        body = self._client.get_object(Bucket=self.bucket, Key=key)["Body"]
        digest = hashlib.sha256()
        total = 0
        for part in iter(lambda: body.read(1024 * 1024), b""):
            total += len(part)
            if total > MAX_OBJECT_BYTES:
                raise ValidationFailedError("stored object exceeds size limit")
            digest.update(part)
        return digest.hexdigest()

    def presigned_url(self, key: str, expires_in: int = 300) -> str:
        url: str = self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=min(expires_in, PRESIGN_MAX_SECONDS),
        )
        return url


@dataclass
class InMemoryObjectStore:
    """테스트용 메모리 저장소. 실제로 올린 횟수를 센다."""

    objects: dict[str, bytes] = field(default_factory=dict)
    puts: int = 0

    def put_if_absent(self, key: str, data: bytes, sha256: str) -> str:
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ValidationFailedError("data does not match declared sha256")
        if key in self.objects:
            if self.objects[key] != data:
                raise ValidationFailedError(f"object {key} exists with different content")
            return "exists"
        self.puts += 1
        self.objects[key] = data
        return "uploaded"

    def read_sha256(self, key: str) -> str:
        return hashlib.sha256(self.objects[key]).hexdigest()

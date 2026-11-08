"""Private R2 storage for official policy originals (S3-compatible, boto3).

Objects are content addressed, so a different source can never overwrite an existing one.
The ARC (외국인등록증) bucket is refused here: this pipeline never reads or writes it.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from typing import Any, Protocol

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
    """``{prefix}/{domain}/sha256/{hex}{ext}`` — proposal pending the backend key contract."""
    if len(sha256) != 64:
        raise ValueError("sha256 must be 64 hex characters")
    ext = extension.lower() if extension.startswith(".") else f".{extension.lower()}"
    return f"{prefix.strip('/')}/{domain}/sha256/{sha256}{ext}"


class ObjectStore(Protocol):
    def put_if_absent(self, key: str, data: bytes, sha256: str) -> str:
        """Return "uploaded" or "exists". Raise if an object exists with other content."""
        ...

    def read_sha256(self, key: str) -> str: ...


class UnconfiguredObjectStore:
    def put_if_absent(self, key: str, data: bytes, sha256: str) -> str:
        raise ContractBlockedError(
            "R2 not configured: set R2_ENDPOINT_URL, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, "
            "R2_POLICY_BUCKET and confirm bucket access and object key contract"
        )

    def read_sha256(self, key: str) -> str:
        raise ContractBlockedError("R2 not configured")


class R2ObjectStore:
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
            # ContentMD5 lets the server reject corrupted uploads; ETag is never used as SHA-256.
            ContentMD5=base64.b64encode(hashlib.md5(data).digest()).decode(),  # noqa: S324
            Metadata={"sha256": sha256},
        )
        return "uploaded"

    def read_sha256(self, key: str) -> str:
        """Download and hash the stored bytes (round-trip verification)."""
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

"""데이터 루트 아래에 수동으로 넣은 소스 파일을 안전하게 찾는다."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

FileKind = Literal["pdf", "hwp", "hwpx", "unknown"]

ALLOWED_EXTENSIONS: dict[str, FileKind] = {".pdf": "pdf", ".hwp": "hwp", ".hwpx": "hwpx"}
IGNORED_NAMES = {".gitkeep", ".DS_Store"}
MAX_SOURCE_BYTES = 50 * 1024 * 1024

_OLE_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sniff_kind(head: bytes) -> FileKind:
    """확장자가 아니라 앞부분 바이트로 실제 컨테이너 형식을 판별한다."""
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(_OLE_MAGIC):
        return "hwp"
    if head.startswith(b"PK\x03\x04"):
        return "hwpx"
    return "unknown"


def resolve_inside(root: Path, relative: str) -> Path:
    """``root`` 아래에서 ``relative``를 해석하고, 그 밖으로 벗어나는 경로는 거부한다.

    심볼릭 링크를 먼저 해석하므로 루트 밖을 가리키는 링크도 거부된다.
    """
    if Path(relative).is_absolute():
        raise ValueError(f"absolute path not allowed: {relative}")
    root_resolved = root.resolve(strict=True)
    candidate = (root_resolved / relative).resolve(strict=False)
    if not candidate.is_relative_to(root_resolved):
        raise ValueError(f"path escapes data root: {relative}")
    return candidate


@dataclass(frozen=True)
class DiscoveredFile:
    """소스 파일 하나의 검사 결과(상태, 형식, 크기, 해시)."""

    relative_path: str
    status: Literal["found", "excluded", "failed"]
    reason: str = ""
    extension_kind: FileKind = "unknown"
    detected_kind: FileKind = "unknown"
    size_bytes: int = 0
    sha256: str = ""


def inspect_file(root: Path, relative: str) -> DiscoveredFile:
    try:
        path = resolve_inside(root, relative)
    except ValueError as exc:
        return DiscoveredFile(relative, "failed", str(exc))
    if path.name in IGNORED_NAMES:
        return DiscoveredFile(relative, "excluded", "placeholder file, not a document")
    ext_kind = ALLOWED_EXTENSIONS.get(path.suffix.lower(), "unknown")
    if ext_kind == "unknown":
        return DiscoveredFile(relative, "excluded", f"extension not allowed: {path.suffix}")
    if not path.is_file():
        return DiscoveredFile(relative, "failed", "file not found", ext_kind)
    size = path.stat().st_size
    if size == 0:
        return DiscoveredFile(relative, "failed", "empty file", ext_kind)
    if size > MAX_SOURCE_BYTES:
        return DiscoveredFile(
            relative, "failed", f"file exceeds {MAX_SOURCE_BYTES} bytes", ext_kind
        )
    data = path.read_bytes()
    detected = sniff_kind(data[:8])
    if detected != ext_kind:
        return DiscoveredFile(
            relative,
            "failed",
            f"content is {detected}, extension says {ext_kind}",
            ext_kind,
            detected,
            size,
            sha256_bytes(data),
        )
    return DiscoveredFile(relative, "found", "", ext_kind, detected, size, sha256_bytes(data))


def discover(root: Path) -> list[DiscoveredFile]:
    """``root``를 순회하며(디렉터리 심볼릭 링크는 따라가지 않음) 모든 파일을 검사한다."""
    root_resolved = root.resolve(strict=True)
    results: list[DiscoveredFile] = []
    for path in sorted(root_resolved.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        relative = path.relative_to(root_resolved).as_posix()
        if path.is_symlink():
            target = path.resolve()
            if not target.is_relative_to(root_resolved):
                results.append(DiscoveredFile(relative, "failed", "symlink escapes data root"))
                continue
        results.append(inspect_file(root_resolved, relative))
    return results

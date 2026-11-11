"""sentence-transformers로 만드는 BGE-M3 dense 임베딩.

모델은 import 시점에 로드하지 않는다. 문서와 질의는 같은 모델 인스턴스, revision,
정규화를 공유하므로 두 벡터를 서로 비교할 수 있다.
"""

from __future__ import annotations

import logging
import os
import platform
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import metadata
from typing import TYPE_CHECKING, Any, Protocol

from app.rag.schemas import EMBEDDING_DIMENSIONS, validate_embedding

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

# BGE-M3는 최대 8192 토큰을 받는다. 청커 상한이 훨씬 낮으므로 여기에 닿으면 버그다.
MODEL_MAX_TOKENS = 8192
# dense BGE-M3에 sentence-transformers가 필요로 하는 파일
# (저장소 전체 ~4.6 GB 대신 ~2.3 GB).
MODEL_FILE_PATTERNS = [
    "config.json",
    "config_sentence_transformers.json",
    "modules.json",
    "sentence_bert_config.json",
    "1_Pooling/config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "sentencepiece.bpe.model",
    "pytorch_model.bin",
]


class Embedder(Protocol):
    """문서·질의 임베딩과 토큰 수 계산을 제공하는 임베더 인터페이스."""

    model_name: str

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...

    def count_tokens(self, text: str) -> int: ...


@dataclass(frozen=True)
class EmbedderInfo:
    """실행 보고서에 기록하는 임베더 모델과 실행 환경 정보."""

    model_name: str
    revision: str
    device: str
    batch_size: int
    normalize: bool
    sentence_transformers: str
    torch: str
    python: str

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class BgeM3Embedder:
    """dense 전용 BGE-M3. Sparse/ColBERT 출력은 만들지도 저장하지도 않는다."""

    def __init__(
        self,
        model_name: str = "BAAI/bge-m3",
        *,
        revision: str = "",
        device: str = "cpu",
        batch_size: int = 8,
        local_files_only: bool = False,
    ) -> None:
        self.model_name = model_name
        self.revision = revision or None
        self.device = device
        self.batch_size = batch_size
        self.local_files_only = local_files_only
        self._model: SentenceTransformer | None = None

    def load(self) -> SentenceTransformer:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            # dense 임베딩에 필요한 파일만 받고, 로드는 항상 로컬 파일에서 한다.
            # 온라인으로 로드하면 transformers가 non-daemon "auto_conversion" 스레드를
            # 띄운다. 이 스레드는 Hub PR에서 ~2.3 GB safetensors 사본을 내려받고
            # (고정한 revision에는 pytorch_model.bin만 있다) 끝날 때까지 프로세스
            # 종료를 막는다.
            self._ensure_local_files()
            model = SentenceTransformer(
                self.model_name,
                revision=self.revision,
                device=self.device,
                local_files_only=True,
                model_kwargs={"use_safetensors": False},
            )
            dim = model.get_embedding_dimension()
            if dim != EMBEDDING_DIMENSIONS:
                raise RuntimeError(f"{self.model_name} outputs {dim} dims, expected 1024")
            # 워밍업: 첫 encode는 ~10배 느리다(CPU 실측 1.8 s 대 ~0.15 s). 그래서 이
            # 비용을 첫 사용자 질의 대신 로드 시점에 치른다.
            model.encode(["warm-up"], normalize_embeddings=True, show_progress_bar=False)
            self._model = model
        return self._model

    def _ensure_local_files(self) -> None:
        """dense 모델 파일을 내려받는다(ONNX, ColBERT/sparse 헤드, 이미지는 건너뛴다)."""
        from huggingface_hub import snapshot_download

        snapshot_download(
            self.model_name,
            revision=self.revision,
            allow_patterns=MODEL_FILE_PATTERNS,
            local_files_only=self.local_files_only,
        )

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def info(self) -> EmbedderInfo:
        def version(package: str) -> str:
            try:
                return metadata.version(package)
            except metadata.PackageNotFoundError:
                return "missing"

        return EmbedderInfo(
            model_name=self.model_name,
            revision=self.revision or "default-branch",
            device=self.device,
            batch_size=self.batch_size,
            normalize=True,
            sentence_transformers=version("sentence-transformers"),
            torch=version("torch"),
            python=platform.python_version(),
        )

    def count_tokens(self, text: str) -> int:
        tokenizer = self.load().tokenizer
        return len(tokenizer(text, add_special_tokens=True)["input_ids"])

    def _encode(self, texts: Sequence[str]) -> list[list[float]]:
        for text in texts:
            if self.count_tokens(text) > MODEL_MAX_TOKENS:
                raise ValueError("text exceeds model max tokens; refusing to truncate")
        model = self.load()
        batch = self.batch_size
        while True:
            try:
                vectors = model.encode(
                    list(texts),
                    batch_size=batch,
                    normalize_embeddings=True,
                    convert_to_numpy=True,
                    show_progress_bar=False,
                )
                break
            except RuntimeError as exc:
                # GPU OOM 시 제한된 백오프: 배치가 1이 될 때까지 절반으로 줄인다.
                if "out of memory" not in str(exc).lower() or batch == 1:
                    raise
                batch = max(1, batch // 2)
                logger.warning("embedding OOM, retrying with batch_size=%d", batch)
                _empty_cuda_cache()
        return [validate_embedding(vector.tolist()) for vector in vectors]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        return self._encode(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._encode([text])[0]


def _empty_cuda_cache() -> None:
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:  # pragma: no cover - torch ships with sentence-transformers
        pass


def local_tokenizer_counter(model_name: str, revision: str = "") -> Any:
    """로컬에 캐시된 토크나이저로 만든 토큰 카운터. 캐시가 없으면 ``None``.

    모델 파일을 내려받거나 네트워크 요청을 하면 안 되는 dry run에서 쓴다.
    """
    # local_files_only만으로는 huggingface_hub가 여전히 hub API를 호출한다. 아래 값은
    # import 시점에 읽히므로 transformers를 import하기 전에 설정한다.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    try:
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(
            model_name, revision=revision or None, local_files_only=True
        )
    except (OSError, ValueError):
        return None

    def count(text: str) -> int:
        return len(tokenizer(text, add_special_tokens=True)["input_ids"])

    return count

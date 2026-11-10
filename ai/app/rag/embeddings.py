"""BGE-M3 dense embeddings via sentence-transformers.

The model is never loaded at import time. Documents and queries share one model instance,
revision and normalization so their vectors are comparable.
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

# BGE-M3 accepts up to 8192 tokens; the chunker caps far lower, so hitting this is a bug.
MODEL_MAX_TOKENS = 8192
# Files sentence-transformers needs for dense BGE-M3 (~2.3 GB instead of ~4.6 GB for the repo).
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
    model_name: str

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...

    def count_tokens(self, text: str) -> int: ...


@dataclass(frozen=True)
class EmbedderInfo:
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
    """Dense-only BGE-M3. Sparse/ColBERT outputs are not produced or stored."""

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

            # Fetch only what dense embedding needs, then always load from local files.
            # Loading online makes transformers start a non-daemon "auto_conversion" thread
            # that downloads a ~2.3 GB safetensors copy from a Hub PR (the pinned revision
            # ships pytorch_model.bin only) and blocks process exit until it finishes.
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
            # Warm-up: the first encode is ~10x slower (measured 1.8 s vs ~0.15 s on CPU), so pay
            # it at load time instead of on the first user query.
            model.encode(["warm-up"], normalize_embeddings=True, show_progress_bar=False)
            self._model = model
        return self._model

    def _ensure_local_files(self) -> None:
        """Download the dense-model files (skips ONNX, ColBERT/sparse heads, images)."""
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
                # Bounded back-off on GPU OOM: halve the batch until 1.
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
    """Token counter from a locally cached tokenizer, or ``None`` if not cached.

    Used by dry runs, which must not download model files or make any network request.
    """
    # local_files_only alone still lets huggingface_hub call the hub API; these are read at
    # import time, so set them before transformers is imported.
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

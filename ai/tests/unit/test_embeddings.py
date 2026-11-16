"""BgeM3Embedder 토큰 수 세기: 모델 없이 토크나이저만 쓴다.

실제 모델·토크나이저·네트워크 없이 실행한다(huggingface_hub, transformers를 가짜로 바꾼다).
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from app.rag.embeddings import MODEL_FILE_PATTERNS, BgeM3Embedder

pytestmark = pytest.mark.unit

REVISION = "5617a9f61b028005a4858fdac845db406aefb181"


class FakeTokenizer:
    """공백으로 나눈 단어 수 + 특수 토큰 2개를 돌려주고 받은 호출을 기록한다."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def __call__(self, text: str, add_special_tokens: bool) -> dict[str, list[int]]:
        self.calls.append((text, add_special_tokens))
        return {"input_ids": [0, *range(len(text.split())), 2]}


@pytest.fixture
def hub(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """가짜 huggingface_hub·transformers. 받은 파일 패턴과 토크나이저 로드 인자를 기록한다."""
    seen: dict[str, Any] = {"downloads": [], "tokenizers": [], "tokenizer": FakeTokenizer()}

    def snapshot_download(
        repo_id: str, *, revision: str | None, allow_patterns: list[str], local_files_only: bool
    ) -> str:
        seen["downloads"].append((repo_id, revision, allow_patterns, local_files_only))
        return "unused"

    def from_pretrained(name: str, *, revision: str | None, local_files_only: bool) -> Any:
        seen["tokenizers"].append((name, revision, local_files_only))
        return seen["tokenizer"]

    monkeypatch.setitem(
        sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=snapshot_download)
    )
    monkeypatch.setitem(
        sys.modules,
        "transformers",
        SimpleNamespace(AutoTokenizer=SimpleNamespace(from_pretrained=from_pretrained)),
    )
    return seen


@pytest.mark.parametrize("local_files_only", [False, True])
def test_count_tokens_loads_only_the_tokenizer(
    hub: dict[str, Any], monkeypatch: pytest.MonkeyPatch, local_files_only: bool
) -> None:
    def no_model(self: BgeM3Embedder) -> None:
        raise AssertionError("counting tokens must not load the embedding model")

    monkeypatch.setattr(BgeM3Embedder, "load", no_model)
    embedder = BgeM3Embedder(revision=REVISION, local_files_only=local_files_only)

    assert embedder.count_tokens("a b c") == 5
    assert embedder.count_tokens("d") == 3
    assert not embedder.loaded
    assert hub["tokenizer"].calls == [("a b c", True), ("d", True)]
    # 토크나이저는 한 번만, 같은 모델·revision으로 로컬 파일에서 불러온다.
    assert hub["tokenizers"] == [("BAAI/bge-m3", REVISION, True)]
    [(repo, revision, patterns, local_only)] = hub["downloads"]
    assert (repo, revision, local_only) == ("BAAI/bge-m3", REVISION, local_files_only)
    # 모델 가중치는 받지 않는다.
    assert "pytorch_model.bin" not in patterns
    assert set(patterns) < set(MODEL_FILE_PATTERNS)


def test_embedding_loads_the_model_and_then_counts_with_its_tokenizer(
    hub: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    model_tokenizer = FakeTokenizer()
    loads: list[int] = []

    class FakeModel:
        tokenizer = model_tokenizer

        def encode(self, texts: list[str], **kwargs: Any) -> Any:
            return np.array([[1.0] + [0.0] * 1023 for _ in texts])

    def fake_load(self: BgeM3Embedder) -> Any:
        if self._model is None:
            loads.append(1)
            self._model = FakeModel()  # type: ignore[assignment]
        return self._model

    monkeypatch.setattr(BgeM3Embedder, "load", fake_load)
    embedder = BgeM3Embedder(revision=REVISION)

    embedder.count_tokens("chunk text")  # 청킹: 토크나이저만
    assert loads == []
    vectors = embedder.embed_documents(["chunk text"])
    assert loads == [1] and len(vectors) == 1 and len(vectors[0]) == 1024
    # 모델을 올린 뒤에는 모델의 토크나이저로 센다(토크나이저를 다시 불러오지 않는다).
    assert embedder.count_tokens("x y") == 4
    assert model_tokenizer.calls[-1] == ("x y", True)
    assert len(hub["tokenizers"]) == 1


def test_too_long_text_is_rejected_without_loading_the_model(
    hub: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_model(self: BgeM3Embedder) -> None:
        raise AssertionError("length check must not load the embedding model")

    monkeypatch.setattr(BgeM3Embedder, "load", no_model)
    embedder = BgeM3Embedder(revision=REVISION)

    with pytest.raises(ValueError, match="max tokens"):
        embedder.embed_documents(["word " * 9000])
    assert not embedder.loaded

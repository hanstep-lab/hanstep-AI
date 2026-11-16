"""토크나이저 전용 토큰 수가 모델 토크나이저(model.tokenizer)와 같은지 실제 BGE-M3로 확인한다.

로컬 Hugging Face 캐시에 모델(고정 revision)이 있어야 한다. 없으면 건너뛴다(네트워크로 받지 않음).
모델을 한 번 올리므로 수십 초 걸린다. CI는 `rag` 마커를 빼고 돌린다.
"""

from __future__ import annotations

import pytest

from app.integrations.law_api import parse_law
from app.rag.embeddings import BgeM3Embedder
from tests.conftest import FIXTURES

pytestmark = [pytest.mark.integration, pytest.mark.rag]

MODEL = "BAAI/bge-m3"
REVISION = "5617a9f61b028005a4858fdac845db406aefb181"


def _texts() -> list[str]:
    law = parse_law((FIXTURES / "law_api" / "lawService_real_excerpt.xml").read_bytes())
    return [
        *(block.text for block in law.blocks[:20]),
        "숙련기능인력(E-7-4) 점수제: 평균소득 2,500만원 이상 3,000만원 미만 50점, TOPIK 3급 이상.",
        "| 항목 | 점수 |\n| --- | ---: |\n| 한국어 | 20 |",
        "Mixed text with English, 숫자 123,456.78, 기호 ①②③ ※ ·, and emoji 😀👍",
        "  앞뒤 공백과\n\n여러 줄\t탭  ",
        "",
        "법령 조문 반복 " * 600,
    ]


@pytest.fixture(scope="module")
def embedder() -> BgeM3Embedder:
    return BgeM3Embedder(MODEL, revision=REVISION, local_files_only=True)


def test_tokenizer_only_counts_match_model_tokenizer(embedder: BgeM3Embedder) -> None:
    texts = _texts()
    try:
        counts = [embedder.count_tokens(text) for text in texts]
    except OSError as exc:  # 로컬 캐시에 토크나이저 파일이 없음
        pytest.skip(f"BLOCKED: {MODEL}@{REVISION} not in local HF cache ({type(exc).__name__})")
    # 토큰 수만 셌으므로 모델은 아직 올라가지 않았다
    assert not embedder.loaded

    try:
        model = embedder.load()
    except OSError as exc:
        pytest.skip(f"BLOCKED: model weights not in local HF cache ({type(exc).__name__})")
    expected = [len(model.tokenizer(text, add_special_tokens=True)["input_ids"]) for text in texts]

    assert counts == expected
    assert max(counts) > 512  # 긴 텍스트도 잘리지 않고 같은 수가 나온다
    # 모델을 올린 뒤의 count_tokens도 같은 값이다(모델 토크나이저 사용)
    assert [embedder.count_tokens(text) for text in texts] == expected

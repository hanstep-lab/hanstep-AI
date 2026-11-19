"""ready의 검색기 구축 상태(이슈 #98).

문서 폴더가 설정된 검색기가 building이면 ready=false. error는 금융 정책 검색기만 ready를 막는다
(상품 검색기는 실패해도 도구가 조건표 순서로 대신한다). 미설정은 ready에 영향이 없다.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.api.routes import health
from app.core.config import Settings
from app.main import AppState, build_state, create_app
from app.rag.embeddings import BgeM3Embedder

pytestmark = pytest.mark.unit

TOKEN = "t" * 40
SPRING_KEY = "s" * 40


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "spring_internal_api_url": "http://localhost:8080",
        "spring_internal_api_key": SPRING_KEY,
        "ai_internal_api_token": TOKEN,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


class _LoadedEmbedder(BgeM3Embedder):
    @property
    def loaded(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _mongo_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    # 다른 확인은 모두 통과시키고 검색기 상태만 본다.
    monkeypatch.setattr(
        health, "_check_mongo", lambda state: {"mongodb": "ok", "vector_index": "match:queryable"}
    )


def _client(state: AppState) -> TestClient:
    app = create_app()
    app.state.rag = state
    return TestClient(app)


def _ready_state(**status: str) -> AppState:
    # 설정된 게이트웨이 자리(ready는 미설정 게이트웨이인지만 본다).
    gateway: Any = object()
    state = AppState(settings=_settings(), gateway=gateway, embedder=_LoadedEmbedder())
    for name, value in status.items():
        setattr(state, name, value)
    return state


def test_not_configured_searchers_do_not_affect_ready() -> None:
    response = _client(_ready_state()).get("/health/ready")
    body = response.json()
    assert response.status_code == 200
    assert body["ready"] is True
    assert body["checks"]["productSearch"] == "not_configured"
    assert body["checks"]["financePolicySearch"] == "not_configured"


@pytest.mark.parametrize("field", ["finance_policy_search_status", "product_search_status"])
def test_building_searcher_blocks_ready(field: str) -> None:
    client = _client(_ready_state(**{field: "building"}))
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["ready"] is False
    # live는 그대로
    assert client.get("/health/live").json() == {"status": "ok"}


@pytest.mark.parametrize("field", ["finance_policy_search_status", "product_search_status"])
def test_ready_searcher_allows_ready(field: str) -> None:
    response = _client(_ready_state(**{field: "ready"})).get("/health/ready")
    assert response.status_code == 200


def test_finance_policy_error_blocks_ready() -> None:
    state = _ready_state(finance_policy_search_status="error")
    state.errors["financePolicySearch"] = "ValueError"
    response = _client(state).get("/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["financePolicySearch"] == "error:ValueError"


def test_product_error_keeps_ready_because_tool_falls_back() -> None:
    state = _ready_state(product_search_status="error")
    state.errors["productSearch"] = "OSError"
    response = _client(state).get("/health/ready")
    assert response.status_code == 200
    assert response.json()["checks"]["productSearch"] == "error:OSError"


def test_build_state_marks_configured_searchers_building(tmp_path: Any) -> None:
    configured = build_state(
        _settings(finance_policy_corpus_dir=str(tmp_path), product_corpus_dir=str(tmp_path))
    )
    assert configured.finance_policy_search_status == "building"
    assert configured.product_search_status == "building"
    plain = build_state(_settings())
    assert plain.finance_policy_search_status == "not_configured"
    assert plain.product_search_status == "not_configured"


def test_build_success_and_failure_set_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    sentinel = object()
    monkeypatch.setattr(main_module.FinancePolicySearch, "build", lambda *a, **k: sentinel)

    def broken(*args: Any, **kwargs: Any) -> None:
        raise ValueError("bad pdf")

    monkeypatch.setattr(main_module.ProductSearch, "build", broken)
    state = build_state(
        _settings(finance_policy_corpus_dir=str(tmp_path), product_corpus_dir=str(tmp_path))
    )
    main_module._build_finance_policy_search(state)
    main_module._build_product_search(state)
    assert state.finance_policy_search is sentinel
    assert state.finance_policy_search_status == "ready"
    assert state.product_search is None
    assert state.product_search_status == "error"
    assert state.errors["productSearch"] == "ValueError"


def test_model_failure_marks_configured_searchers_error(tmp_path: Any) -> None:
    class Broken(BgeM3Embedder):
        def load(self) -> Any:
            raise OSError("hub unreachable")

    state = build_state(_settings(finance_policy_corpus_dir=str(tmp_path)))
    state.embedder = Broken()
    main_module._load_model(state)
    assert state.finance_policy_search_status == "error"
    assert state.errors["financePolicySearch"] == "ModelNotLoaded"
    # 미설정 검색기는 그대로
    assert state.product_search_status == "not_configured"


def test_ready_turns_true_only_after_search_is_built(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """모델 로드 → (구축 중 ready=false) → 구축 끝 → ready=true. 운영 장애(11/18)의 재현과 수정."""
    release = threading.Event()

    def fake_load(self: BgeM3Embedder) -> None:
        self._model = object()  # type: ignore[assignment]

    def slow_build(*args: Any, **kwargs: Any) -> object:
        release.wait(timeout=5)
        return object()

    monkeypatch.setattr(BgeM3Embedder, "load", fake_load)
    monkeypatch.setattr(main_module.FinancePolicySearch, "build", slow_build)
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: _settings(load_model_on_startup=True, finance_policy_corpus_dir=str(tmp_path)),
    )
    with TestClient(main_module.create_app()) as client:
        for _ in range(100):
            body = client.get("/health/ready").json()
            if body["checks"]["model"] == "loaded":
                break
            threading.Event().wait(0.02)
        assert body["checks"]["model"] == "loaded"
        assert body["checks"]["financePolicySearch"] == "building"
        assert body["ready"] is False
        release.set()
        for _ in range(100):
            response = client.get("/health/ready")
            if response.json()["checks"]["financePolicySearch"] == "ready":
                break
            threading.Event().wait(0.02)
        assert response.json()["checks"]["financePolicySearch"] == "ready"
        assert response.status_code == 200

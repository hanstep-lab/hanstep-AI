from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.integrations.policy_gateway import UnconfiguredPolicyGateway
from app.integrations.r2 import InMemoryObjectStore, R2ObjectStore, object_key
from app.main import AppState, create_app
from app.rag.embeddings import BgeM3Embedder
from app.rag.errors import ValidationFailedError

pytestmark = pytest.mark.unit

KEY = "test-internal-key-123"


@pytest.fixture
def client() -> TestClient:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        spring_internal_api_url="http://localhost:8080",
        spring_internal_api_key=KEY,  # type: ignore[arg-type]
    )
    app = create_app()
    # No lifespan: no MongoDB, no model download.
    app.state.rag = AppState(
        settings=settings, gateway=UnconfiguredPolicyGateway(), embedder=BgeM3Embedder()
    )
    return TestClient(app)


def _headers() -> dict[str, str]:
    return {"X-Internal-Api-Key": KEY}


def test_live(client: TestClient) -> None:
    assert client.get("/health/live").json() == {"status": "ok"}


def test_ready_is_503_without_dependencies(client: TestClient) -> None:
    response = client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["checks"]["mongodb"] == "not_configured"
    assert KEY not in response.text


def test_search_requires_auth(client: TestClient) -> None:
    assert client.post("/internal/rag/search", json={"query": "E-7-4"}).status_code == 401
    wrong = client.post(
        "/internal/rag/search", json={"query": "q"}, headers={"X-Internal-Api-Key": "x"}
    )
    assert wrong.status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        {"query": ""},
        {"query": "q", "top_k": 0},
        {"query": "q", "top_k": 21},
        {"query": "x" * 1001},
        {"query": "q", "filter": {"is_active": False}},
        {"query": "q", "pipeline": []},
    ],
)
def test_search_rejects_invalid_or_filter_override(
    client: TestClient, body: dict[str, Any]
) -> None:
    assert client.post("/internal/rag/search", json=body, headers=_headers()).status_code == 422


def test_finance_search_not_supported(client: TestClient) -> None:
    response = client.post(
        "/internal/rag/search",
        json={"query": "SOL Global savings", "domain": "finance"},
        headers=_headers(),
    )
    assert response.status_code == 501


def test_search_without_vector_store_is_unavailable(client: TestClient) -> None:
    response = client.post("/internal/rag/search", json={"query": "q"}, headers=_headers())
    assert response.status_code == 503


def test_ingestion_endpoints_closed_until_job_contract(client: TestClient) -> None:
    created = client.post(
        "/internal/rag/ingestions", json={"idempotency_key": "abcdefgh"}, headers=_headers()
    )
    assert created.status_code == 503
    assert "ai_jobs" in created.json()["detail"]
    assert client.get("/internal/rag/ingestions/x", headers=_headers()).status_code == 503


def test_r2_key_is_content_addressed() -> None:
    key = object_key("policy-sources/", "visa", "a" * 64, "PDF")
    assert key == f"policy-sources/visa/sha256/{'a' * 64}.pdf"
    with pytest.raises(ValueError):
        object_key("p", "visa", "abc", ".pdf")


def test_in_memory_store_refuses_overwrite() -> None:
    import hashlib

    store = InMemoryObjectStore()
    data = b"original"
    sha = hashlib.sha256(data).hexdigest()
    assert store.put_if_absent("k", data, sha) == "uploaded"
    assert store.put_if_absent("k", data, sha) == "exists"
    other = b"changed"
    with pytest.raises(ValidationFailedError):
        store.put_if_absent("k", other, hashlib.sha256(other).hexdigest())
    with pytest.raises(ValidationFailedError):
        store.put_if_absent("k2", data, "0" * 64)


def test_r2_store_refuses_arc_bucket() -> None:
    with pytest.raises(ValueError, match="ARC"):
        R2ObjectStore(
            endpoint_url="https://acc.r2.cloudflarestorage.com",
            access_key_id="a",
            secret_access_key="b",
            bucket="shinhan-arc-private",
            forbidden_buckets=("shinhan-arc-private",),
        )

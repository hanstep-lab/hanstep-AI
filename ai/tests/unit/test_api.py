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

TOKEN = "t" * 40
SPRING_KEY = "s" * 40


def _make_client(token: str = TOKEN) -> TestClient:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        spring_internal_api_url="http://localhost:8080",
        spring_internal_api_key=SPRING_KEY,  # type: ignore[arg-type]
        ai_internal_api_token=token,  # type: ignore[arg-type]
    )
    app = create_app()
    # No lifespan: no MongoDB, no model download.
    app.state.rag = AppState(
        settings=settings, gateway=UnconfiguredPolicyGateway(), embedder=BgeM3Embedder()
    )
    return TestClient(app)


@pytest.fixture
def client() -> TestClient:
    return _make_client()


def _headers() -> dict[str, str]:
    return {"X-Internal-Token": TOKEN}


def test_live(client: TestClient) -> None:
    assert client.get("/health/live").json() == {"status": "ok"}


def test_ready_is_503_without_dependencies(client: TestClient) -> None:
    response = client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["checks"]["mongodb"] == "not_configured"
    assert TOKEN not in response.text
    assert body["checks"]["internal_auth"] == "configured"


INTERNAL_ROUTES = [
    ("post", "/internal/v1/rag/search", {"query": "E-7-4"}),
    ("post", "/internal/v1/rag/ingestions", {"idempotency_key": "abcdefgh"}),
    ("get", "/internal/v1/rag/ingestions/x", None),
]


@pytest.mark.parametrize(("method", "path", "body"), INTERNAL_ROUTES)
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Internal-Token": "wrong"},
        {"X-Internal-Token": ""},
        # The outbound Spring key must not open the inbound API.
        {"X-Internal-Token": SPRING_KEY},
        # Right token in the wrong header.
        {"X-Internal-Api-Key": TOKEN},
    ],
)
def test_internal_routes_reject_bad_credentials(
    client: TestClient, method: str, path: str, body: Any, headers: dict[str, str]
) -> None:
    response = client.request(method, path, json=body, headers=headers)
    assert response.status_code == 401
    assert TOKEN not in response.text


@pytest.mark.parametrize("token", ["", "short-token", "CHANGE_ME_AI_INTERNAL_API_TOKEN" + "x" * 10])
def test_unconfigured_or_weak_token_rejects_everything(token: str) -> None:
    client = _make_client(token)
    for method, path, body in INTERNAL_ROUTES:
        response = client.request(method, path, json=body, headers={"X-Internal-Token": token})
        assert response.status_code == 401


def test_old_unversioned_paths_are_gone(client: TestClient) -> None:
    response = client.post("/internal/rag/search", json={"query": "q"}, headers=_headers())
    assert response.status_code == 404


def test_health_needs_no_token(client: TestClient) -> None:
    assert client.get("/health/live").status_code == 200


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
    assert client.post("/internal/v1/rag/search", json=body, headers=_headers()).status_code == 422


def test_finance_search_not_supported(client: TestClient) -> None:
    response = client.post(
        "/internal/v1/rag/search",
        json={"query": "SOL Global savings", "domain": "finance"},
        headers=_headers(),
    )
    assert response.status_code == 501


def test_search_without_vector_store_is_unavailable(client: TestClient) -> None:
    response = client.post("/internal/v1/rag/search", json={"query": "q"}, headers=_headers())
    assert response.status_code == 503


def test_ingestion_endpoints_closed_until_job_contract(client: TestClient) -> None:
    created = client.post(
        "/internal/v1/rag/ingestions", json={"idempotency_key": "abcdefgh"}, headers=_headers()
    )
    assert created.status_code == 503
    assert "ai_jobs" in created.json()["detail"]
    assert client.get("/internal/v1/rag/ingestions/x", headers=_headers()).status_code == 503


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


def _r2_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "r2_endpoint_url": "https://acc.r2.cloudflarestorage.com",
        "r2_access_key_id": "key-id",
        "r2_secret_access_key": "secret",
        "r2_policy_bucket": "hanstep-visa-policies",
        "r2_product_bucket": "hanstep-financial-products",
        "r2_arc_bucket": "hanstep-arc-private",
    }
    values.update(overrides)
    return Settings(**values)


def test_buckets_are_split_by_domain() -> None:
    from app.integrations.r2 import build_object_stores

    settings = _r2_settings()
    assert settings.r2_bucket_for("visa") == "hanstep-visa-policies"
    assert settings.r2_bucket_for("finance") == "hanstep-financial-products"
    assert settings.r2_bucket_for("arc") == ""
    stores = build_object_stores(settings)
    assert {d: getattr(s, "bucket", None) for d, s in stores.items()} == {
        "visa": "hanstep-visa-policies",
        "finance": "hanstep-financial-products",
    }


def test_same_bucket_for_two_purposes_is_rejected() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="different"):
        _r2_settings(r2_product_bucket="hanstep-arc-private")
    with pytest.raises(ValidationError, match="different"):
        _r2_settings(r2_product_bucket="hanstep-visa-policies")


def test_missing_credentials_block_every_domain() -> None:
    from app.integrations.r2 import UnconfiguredObjectStore, build_object_stores
    from app.rag.errors import ContractBlockedError

    stores = build_object_stores(_r2_settings(r2_secret_access_key="your-r2-secret-key"))
    assert all(isinstance(s, UnconfiguredObjectStore) for s in stores.values())
    with pytest.raises(ContractBlockedError, match="R2_PRODUCT_BUCKET"):
        stores["finance"].put_if_absent("k", b"x", "0" * 64)


@pytest.mark.parametrize(
    "endpoint",
    ["https://acc.r2.cloudflarestorage.com/hanstep", "https://acc.r2.cloudflarestorage.com/a/b"],
)
def test_r2_endpoint_with_bucket_path_is_rejected(endpoint: str) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="without a bucket name or path"):
        _r2_settings(r2_endpoint_url=endpoint)


def test_r2_endpoint_trailing_slash_is_normalized() -> None:
    settings = _r2_settings(r2_endpoint_url="https://acc.r2.cloudflarestorage.com/")
    assert settings.r2_endpoint_url == "https://acc.r2.cloudflarestorage.com"

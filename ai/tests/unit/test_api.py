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


SEARCH = "/internal/v1/visa/policies/search"
INTERNAL_ROUTES = [("post", SEARCH, {"query": "E-7-4"})]


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
    assert response.json()["code"] == "UNAUTHORIZED"
    assert TOKEN not in response.text


@pytest.mark.parametrize("token", ["", "short-token", "CHANGE_ME_AI_INTERNAL_API_TOKEN" + "x" * 10])
def test_unconfigured_or_weak_token_rejects_everything(token: str) -> None:
    client = _make_client(token)
    for method, path, body in INTERNAL_ROUTES:
        response = client.request(method, path, json=body, headers={"X-Internal-Token": token})
        assert response.status_code == 401


@pytest.mark.parametrize(
    "path", ["/internal/rag/search", "/internal/v1/rag/search", "/internal/v1/rag/ingestions"]
)
def test_old_paths_are_gone(client: TestClient, path: str) -> None:
    response = client.post(path, json={"query": "q"}, headers=_headers())
    assert response.status_code == 404
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"


def test_ready_reports_unconfigured_gateway_even_with_spring_env(client: TestClient) -> None:
    # Spring URL/key are set in the fixture, but the gateway is still the unconfigured one.
    checks = client.get("/health/ready").json()["checks"]
    assert checks["policy_gateway"] == "blocked"


def test_health_needs_no_token(client: TestClient) -> None:
    assert client.get("/health/live").status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {"query": ""},
        {"query": "q", "topK": 0},
        {"query": "q", "topK": 21},
        {"query": "x" * 1001},
        {"query": "q", "top_k": 5},  # snake_case is rejected: JSON is camelCase
        {"query": "q", "filter": {"isActive": False}},
        {"query": "q", "pipeline": []},
        {"query": "q", "domain": "finance"},
    ],
)
def test_search_rejects_invalid_or_filter_override(
    client: TestClient, body: dict[str, Any]
) -> None:
    response = client.post(SEARCH, json=body, headers=_headers())
    assert response.status_code == 400
    payload = response.json()
    assert payload["code"] == "VALIDATION_ERROR"
    assert payload["details"]["fields"]
    assert payload["requestId"].startswith("req_")
    assert "x" * 50 not in response.text  # submitted values are not echoed


def test_error_envelope_reuses_caller_request_id(client: TestClient) -> None:
    response = client.post(
        SEARCH, json={"query": ""}, headers={**_headers(), "X-Request-Id": "req_spring_123"}
    )
    assert response.json()["requestId"] == "req_spring_123"
    assert response.headers["X-Request-Id"] == "req_spring_123"


def test_search_without_vector_store_is_unavailable(client: TestClient) -> None:
    response = client.post(SEARCH, json={"query": "q", "topK": 3}, headers=_headers())
    assert response.status_code == 503
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"


def test_search_response_is_camel_case() -> None:
    from datetime import UTC, date, datetime

    from bson import ObjectId

    from app.api.routes.visa_policies import PolicySearchResult
    from app.rag.retrieval import to_hit

    oid = ObjectId()
    hit = to_hit(
        {
            "_id": oid,
            "policy_document_id": 7,
            "document_version": "E74-2026-v1",
            "chunk_index": 2,
            "chunk_text": "본문",
            "title": "선발 계획",
            "source_url": "https://www.moj.go.kr/x",
            "source_org": "법무부",
            "visa_types": ["E-7-4"],
            "source_type": "notice",
            "issued_at": datetime(2026, 5, 31, 15, 0, tzinfo=UTC),
            "score": 0.8,
            "page_number": 3,
            "effective_from": datetime(2026, 5, 31, 15, 0, tzinfo=UTC),  # KST 2026-06-01
        }
    )
    body = PolicySearchResult.from_hit(hit).model_dump(mode="json", by_alias=True)
    assert body["chunkId"] == str(oid)
    assert body["policyDocumentId"] == 7
    assert body["effectiveFrom"] == date(2026, 6, 1).isoformat()
    assert body["effectiveTo"] is None
    assert (body["sourceType"], body["issuedOn"]) == ("notice", "2026-06-01")
    assert {"sourceUrl", "sourceOrg", "visaTypes", "pageNumber", "sectionTitle"} <= set(body)
    assert not any("_" in key for key in body)


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


@pytest.mark.parametrize(
    "revision", ["", "main", "5617a9f", "5617a9f61b028005a4858fdac845db406aefb181"]
)
def test_embedding_revision_accepts_commit_or_branch(revision: str) -> None:
    assert (
        Settings(_env_file=None, embedding_model_revision=revision).embedding_model_revision
        == revision
    )  # type: ignore[call-arg]


@pytest.mark.parametrize("revision", ["password123", "v1.0", "5617A9F", "../x"])
def test_embedding_revision_rejects_other_values(revision: str) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="commit hash"):
        Settings(_env_file=None, embedding_model_revision=revision)  # type: ignore[call-arg]

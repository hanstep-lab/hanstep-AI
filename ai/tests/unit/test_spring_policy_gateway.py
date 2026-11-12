from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.integrations.spring_policy_gateway import SpringGatewayError, SpringPolicyGateway
from app.rag.errors import ContractBlockedError
from app.rag.retrieval import SearchUnavailableError, filter_current

pytestmark = pytest.mark.unit

KEY = "s" * 40


def _gateway(handler: Any) -> SpringPolicyGateway:
    return SpringPolicyGateway(
        base_url="http://spring.test",
        api_key=KEY,
        client=httpx.Client(base_url="http://spring.test", transport=httpx.MockTransport(handler)),
    )


def _doc(pid: int, version: str, searchable: bool, status: str = "APPROVED") -> dict[str, Any]:
    return {
        "policyDocumentId": pid,
        "policyVersion": version,
        "reviewStatus": status,
        "effectiveFrom": None,
        "effectiveTo": None,
        "searchable": searchable,
    }


def test_current_versions_keeps_only_searchable_and_sends_token() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(
            path=request.url.path,
            ids=request.url.params["ids"],
            token=request.headers["X-Internal-Token"],
        )
        return httpx.Response(
            200,
            json={
                "documents": [
                    _doc(4, "E74-MAFRA-2026-163", True),
                    _doc(6, "E74-SELECTION-2026-20260601", False, "DRAFT"),
                ]
            },
        )

    assert _gateway(handler).current_versions([6, 4, 4, 999]) == {4: "E74-MAFRA-2026-163"}
    assert seen == {
        "path": "/internal/v1/ai-context/policy-documents/search-eligibility",
        "ids": "4,6,999",
        "token": KEY,
    }


def test_current_versions_splits_more_than_max_ids_into_calls() -> None:
    calls: list[list[int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ids = [int(i) for i in request.url.params["ids"].split(",")]
        calls.append(ids)
        return httpx.Response(200, json={"documents": [_doc(i, f"v{i}", i != 3) for i in ids]})

    current = _gateway(handler).current_versions(range(45, 0, -1))

    assert [len(c) for c in calls] == [20, 20, 5]
    assert sorted(i for c in calls for i in c) == list(range(1, 46))
    assert current == {i: f"v{i}" for i in range(1, 46) if i != 3}


def test_current_versions_fails_when_any_split_call_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["ids"].startswith("21,"):
            return httpx.Response(500)
        ids = [int(i) for i in request.url.params["ids"].split(",")]
        return httpx.Response(200, json={"documents": [_doc(i, f"v{i}", True) for i in ids]})

    with pytest.raises(SpringGatewayError):
        _gateway(handler).current_versions(range(1, 26))


def test_no_ids_means_no_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call Spring")

    assert _gateway(handler).current_versions([]) == {}


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"code": "UNAUTHORIZED"}),
        httpx.Response(500),
        httpx.Response(200, text="<html>login</html>"),
        httpx.Response(200, json={"items": []}),
        httpx.Response(
            200,
            json={
                "documents": [
                    {"policyDocumentId": "4", "searchable": True, "reviewStatus": "APPROVED"}
                ]
            },
        ),
        httpx.Response(
            200,
            json={
                "documents": [
                    {"policyDocumentId": 4, "searchable": "yes", "reviewStatus": "APPROVED"}
                ]
            },
        ),
        httpx.Response(
            200,
            json={
                "documents": [
                    {"policyDocumentId": 4, "searchable": True, "reviewStatus": "approved"}
                ]
            },
        ),
    ],
)
def test_bad_answers_raise(response: httpx.Response) -> None:
    with pytest.raises(SpringGatewayError):
        _gateway(lambda request: response).current_versions([4])


def test_network_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(SpringGatewayError):
        _gateway(handler).current_versions([4])


def test_search_fails_closed_when_spring_is_down() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    hit = {
        "policy_document_id": 4,
        "document_version": "v",
        "is_active": True,
        "review_status": "approved",
    }
    with pytest.raises(SearchUnavailableError):
        filter_current([hit], _gateway(handler))


def test_stale_version_in_index_is_dropped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"documents": [_doc(4, "new-version", True)]})

    hits = [
        {
            "policy_document_id": 4,
            "document_version": "old-version",
            "is_active": True,
            "review_status": "approved",
        },
        {
            "policy_document_id": 4,
            "document_version": "new-version",
            "is_active": True,
            "review_status": "approved",
        },
    ]
    assert filter_current(hits, _gateway(handler)) == [hits[1]]


def test_document_lookup_is_not_available_through_spring() -> None:
    gateway = _gateway(lambda request: httpx.Response(200, json={"documents": []}))
    with pytest.raises(ContractBlockedError):
        gateway.get_version(4, "v")


def test_unconfigured_blocks() -> None:
    with pytest.raises(ContractBlockedError):
        SpringPolicyGateway(base_url="", api_key="")

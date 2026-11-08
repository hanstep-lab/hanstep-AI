from __future__ import annotations

import logging

import httpx
import pytest

from app.integrations.law_api import (
    LawApiClient,
    LawApiError,
    law_extraction_report,
    parse_law,
    parse_search,
    select_exact,
)
from app.rag.errors import ContractBlockedError, ValidationFailedError
from tests.conftest import FIXTURES

pytestmark = pytest.mark.unit

LAW = FIXTURES / "law_api"
OC = "secret-oc-value"


def _search_page(total: int, msts: list[str]) -> bytes:
    items = "".join(
        f"<law><법령일련번호>{m}</법령일련번호><현행연혁코드>연혁</현행연혁코드>"
        f"<법령명한글>법{m}</법령명한글><법령ID>{m}</법령ID></law>"
        for m in msts
    )
    return f"<LawSearch><totalCnt>{total}</totalCnt><page>1</page>{items}</LawSearch>".encode()


def _client(handler: httpx.MockTransport, sleeps: list[float] | None = None) -> LawApiClient:
    record = sleeps if sleeps is not None else []
    return LawApiClient(
        base_url="https://www.law.go.kr",
        oc=OC,
        client=httpx.Client(base_url="https://www.law.go.kr", transport=handler),
        sleep=record.append,
    )


def test_parse_fixture_search_and_exact_selection() -> None:
    total, laws = parse_search((LAW / "lawSearch.xml").read_bytes())
    assert total == 4
    chosen = select_exact(laws, "출입국관리법")
    assert chosen.mst == "900001"  # the 현행 one, not the first 연혁 duplicate
    with pytest.raises(ValidationFailedError):
        select_exact(laws, "출입국관리")


def test_parse_fixture_body_keeps_articles_addenda_annexes() -> None:
    doc = parse_law((LAW / "lawService_900001.xml").read_bytes())
    assert (doc.law_id, doc.effective_date) == ("000001", "20260701")
    assert (doc.article_count, doc.addenda_count, doc.annex_count) == (2, 1, 1)
    text = "\n".join(b.text for b in doc.blocks)
    assert "가. 픽스처용 목 내용입니다." in text
    assert "부칙 <제1호, 2026. 1. 1.>" in text
    assert law_extraction_report(doc).status == "ok"
    assert doc.content_sha256() != doc.raw_sha256


def test_pagination_collects_all_pages() -> None:
    pages = {1: [str(i) for i in range(100)], 2: [str(i) for i in range(100, 150)]}

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return httpx.Response(200, content=_search_page(150, pages[page]))

    laws = _client(httpx.MockTransport(handler)).search_all("법")
    assert len(laws) == 150


def test_repeated_page_detected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_search_page(150, [str(i) for i in range(100)]))

    with pytest.raises(LawApiError, match="repeats"):
        _client(httpx.MockTransport(handler)).search_all("법")


def test_429_respects_retry_after_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, content=_search_page(1, ["1"]))

    sleeps: list[float] = []
    assert len(_client(httpx.MockTransport(handler), sleeps).search_all("법")) == 1
    assert sleeps == [7.0]


def test_5xx_retry_is_bounded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    sleeps: list[float] = []
    with pytest.raises(LawApiError, match="503"):
        _client(httpx.MockTransport(handler), sleeps).search_all("법")
    assert len(sleeps) == 3


def test_auth_error_not_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401)

    with pytest.raises(LawApiError):
        _client(httpx.MockTransport(handler)).search_all("법")
    assert calls["n"] == 1


@pytest.mark.parametrize(
    "body",
    [
        b"<!DOCTYPE html><html><body>login</body></html>",
        b"<OpenAPI_ServiceResponse><returnAuthMsg>error</returnAuthMsg></OpenAPI_ServiceResponse>",
        b"<LawSearch><page>1</page></LawSearch>",
        b"not xml",
    ],
)
def test_http_200_error_bodies_rejected(body: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    with pytest.raises(LawApiError):
        _client(httpx.MockTransport(handler)).search_all("법")


def test_redirect_is_not_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://evil.example/"})

    with pytest.raises(LawApiError, match="redirect"):
        _client(httpx.MockTransport(handler)).search_all("법")


def test_oc_never_in_errors_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["OC"] == OC
        return httpx.Response(503)

    caplog.set_level(logging.DEBUG)
    with pytest.raises(LawApiError) as excinfo:
        _client(httpx.MockTransport(handler)).search_all("법")
    assert OC not in str(excinfo.value)
    assert OC not in caplog.text


def test_missing_oc_blocks() -> None:
    with pytest.raises(ContractBlockedError):
        LawApiClient(base_url="https://www.law.go.kr", oc="")


def test_collect_targets_end_to_end_with_fixtures() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("lawSearch.do"):
            return httpx.Response(200, content=(LAW / "lawSearch.xml").read_bytes())
        mst = request.url.params["MST"]
        return httpx.Response(200, content=(LAW / f"lawService_{mst}.xml").read_bytes())

    docs = list(_client(httpx.MockTransport(handler)).collect_targets(("출입국관리법",)))
    assert [d.name for d in docs] == ["출입국관리법"]

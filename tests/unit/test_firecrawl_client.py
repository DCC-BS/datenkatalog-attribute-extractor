"""Tests for the Firecrawl scrape client.

The response bodies here are the shapes a running Firecrawl 2.10.19 actually returned,
including the awkward one where a page that answered 503 still came back as HTTP 200 with
`success: true`.
"""

import json

import httpx
import pytest

from datenkatalog_attribute_extractor.services.web.firecrawl_client import (
    FirecrawlClient,
    FirecrawlUnavailableError,
    PageUnreadableError,
)

SCRAPE_URL = "http://firecrawl-api:3002/v2/scrape"
PAGE_URL = "https://example.org/formular"

FORM_HTML = (
    '<html><body><form><label for="fname">First name:</label>'
    '<input type="text" id="fname" name="fname"></form></body></html>'
)


def build_client(handler) -> FirecrawlClient:
    """Build a client whose transport is a canned handler, so no network is touched."""
    return FirecrawlClient(
        scrape_url=SCRAPE_URL,
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )


def ok_body(*, html: str = FORM_HTML, status: int = 200, warning: str | None = None) -> dict:
    """A successful Firecrawl response body."""
    data: dict = {
        "rawHtml": html,
        "metadata": {"statusCode": status, "sourceURL": PAGE_URL, "title": "Formular"},
    }
    if warning is not None:
        data["warning"] = warning
    return {"success": True, "data": data}


def responder(body: dict, http_status: int = 200):
    """A handler returning one canned JSON body."""

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(http_status, json=body)

    return handle


async def test_scrape_returns_the_raw_html() -> None:
    page = await build_client(responder(ok_body())).scrape(PAGE_URL)

    assert "<input" in page.html
    assert page.title == "Formular"
    assert page.warnings == []


async def test_scrape_requests_raw_html_and_not_markdown() -> None:
    """Markdown drops every form control, so the client must never ask for it."""
    seen: dict = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=ok_body())

    await build_client(handle).scrape(PAGE_URL)

    assert seen["formats"] == ["rawHtml"]
    assert seen["url"] == PAGE_URL
    assert seen["onlyMainContent"] is False


async def test_scrape_sends_no_authorization_header() -> None:
    """Self-hosted Firecrawl runs with USE_DB_AUTHENTICATION=false."""
    seen: dict = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=ok_body())

    await build_client(handle).scrape(PAGE_URL)

    assert seen["auth"] is None


async def test_a_page_that_answered_an_error_status_is_page_local_not_fatal() -> None:
    """Firecrawl returns HTTP 200 and success:true for a page that itself failed.

    Reading only the envelope would hand a "503 Service Temporarily Unavailable" error page
    to the model and call the run a success.
    """
    body = ok_body(html="<html><head><title>503</title></head><body></body></html>", status=503)
    body["data"]["metadata"]["error"] = "Service Unavailable"

    with pytest.raises(PageUnreadableError, match="503"):
        await build_client(responder(body)).scrape(PAGE_URL)


async def test_a_partial_scrape_warning_is_surfaced() -> None:
    warning = "The engine used does not support the following features: screenshot"
    page = await build_client(responder(ok_body(warning=warning))).scrape(PAGE_URL)

    assert len(page.warnings) == 1
    assert "screenshot" in page.warnings[0]


async def test_empty_html_is_page_local() -> None:
    with pytest.raises(PageUnreadableError, match="no HTML"):
        await build_client(responder(ok_body(html="   "))).scrape(PAGE_URL)


async def test_missing_raw_html_is_page_local() -> None:
    body = {"success": True, "data": {"metadata": {"statusCode": 200}}}

    with pytest.raises(PageUnreadableError, match="no HTML"):
        await build_client(responder(body)).scrape(PAGE_URL)


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_firecrawl_server_errors_are_fatal(status: int) -> None:
    with pytest.raises(FirecrawlUnavailableError, match=str(status)):
        await build_client(responder({}, http_status=status)).scrape(PAGE_URL)


async def test_firecrawl_reporting_failure_is_fatal() -> None:
    body = {"success": False, "error": "Actions are not supported by any available engines."}

    with pytest.raises(FirecrawlUnavailableError, match="Actions are not supported"):
        await build_client(responder(body)).scrape(PAGE_URL)


async def test_unreachable_firecrawl_is_fatal() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    with pytest.raises(FirecrawlUnavailableError, match="Connection refused"):
        await build_client(handle).scrape(PAGE_URL)


async def test_a_timeout_is_fatal() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    with pytest.raises(FirecrawlUnavailableError):
        await build_client(handle).scrape(PAGE_URL)


async def test_a_non_json_response_is_fatal() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    with pytest.raises(FirecrawlUnavailableError, match="not valid JSON"):
        await build_client(handle).scrape(PAGE_URL)


async def test_a_response_without_data_is_fatal() -> None:
    with pytest.raises(FirecrawlUnavailableError, match="no data"):
        await build_client(responder({"success": True})).scrape(PAGE_URL)

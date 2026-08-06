"""Tests for the browser client: the contract with the render service, and its failure split.

The split matters as much as the parsing. A browser that cannot be reached is an outage and
aborts the run with a 503; a page that will not load describes the request and comes back as a
400. Getting that wrong either hides a broken deployment or blames one for a typo in a URL.
"""

import base64
import json

import httpx
import pytest

from datenkatalog_attribute_extractor.services.web.browser_client import (
    BrowserClient,
    BrowserUnavailableError,
    PageUnreadableError,
)

OBSERVE_URL = "http://browser:3100/observe"
PAGE_URL = "https://example.org/formular"

TILE = base64.b64encode(b"\x89PNG\r\n\x1a\n fake").decode()

CONTROL = {
    "kind": "text",
    "label": "Vorname",
    "label_source": "layout",
    "context_path": ["Anmeldung"],
    "name": "vorname",
    "options": [],
    "in_form": True,
}


def build_client(handler, *, wait_ms: int = 8000) -> BrowserClient:
    """A client whose transport is a canned handler, so no browser is touched."""
    return BrowserClient(
        observe_url=OBSERVE_URL,
        timeout_seconds=5,
        wait_ms=wait_ms,
        max_tiles=12,
        transport=httpx.MockTransport(handler),
    )


def ok_body(**overrides) -> dict:
    """A successful observation."""
    return {
        "url": PAGE_URL,
        "status": 200,
        "title": "Formular",
        "text": "Formular",
        "controls": [CONTROL],
        "screenshots": [TILE],
    } | overrides


def responder(body: dict, http_status: int = 200):
    """A handler returning one canned JSON body."""

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(http_status, json=body)

    return handle


async def test_an_observation_becomes_controls_and_screenshots() -> None:
    page = await build_client(responder(ok_body())).observe(PAGE_URL)

    assert page.title == "Formular"
    assert [control.label for control in page.controls] == ["Vorname"]
    assert page.controls[0].label_source == "layout"
    assert page.screenshots == [base64.b64decode(TILE)]


async def test_the_render_wait_is_sent() -> None:
    """A client-rendered form is a loading shell without it — no controls, and no error."""
    seen: dict = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=ok_body())

    await build_client(handle, wait_ms=5000).observe(PAGE_URL)

    assert seen["waitMs"] == 5000
    assert seen["url"] == PAGE_URL
    assert seen["screenshots"] is True


async def test_the_labelled_share_is_measured_over_all_controls() -> None:
    unlabelled = CONTROL | {"label": "", "label_source": ""}
    body = ok_body(controls=[CONTROL, unlabelled, unlabelled, unlabelled])

    page = await build_client(responder(body)).observe(PAGE_URL)

    assert page.labelled_share == 0.25


async def test_a_page_with_no_controls_has_a_labelled_share_of_zero() -> None:
    page = await build_client(responder(ok_body(controls=[]))).observe(PAGE_URL)

    assert page.labelled_share == 0.0


async def test_a_page_that_answered_an_error_status_is_page_local_not_fatal() -> None:
    """The service rendered something; that the page was a 404 page is the caller's problem."""
    with pytest.raises(PageUnreadableError, match="404"):
        await build_client(responder(ok_body(status=404))).observe(PAGE_URL)


async def test_a_navigation_failure_is_page_local_not_fatal() -> None:
    body = {"error": "net::ERR_NAME_NOT_RESOLVED at https://nope.example", "kind": "navigation"}

    with pytest.raises(PageUnreadableError, match="ERR_NAME_NOT_RESOLVED"):
        await build_client(responder(body, http_status=502)).observe(PAGE_URL)


@pytest.mark.parametrize("status", [500, 503])
async def test_browser_service_errors_are_fatal(status: int) -> None:
    with pytest.raises(BrowserUnavailableError, match=str(status)):
        await build_client(responder({"error": "boom"}, http_status=status)).observe(PAGE_URL)


async def test_an_unreachable_browser_is_fatal() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    with pytest.raises(BrowserUnavailableError, match="Connection refused"):
        await build_client(handle).observe(PAGE_URL)


async def test_a_timeout_is_fatal() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    with pytest.raises(BrowserUnavailableError):
        await build_client(handle).observe(PAGE_URL)


async def test_a_non_json_response_is_fatal() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    with pytest.raises(BrowserUnavailableError, match="not valid JSON"):
        await build_client(handle).observe(PAGE_URL)


async def test_a_response_without_controls_is_fatal() -> None:
    """No `controls` key at all is a broken service, not a page without fields."""
    body = {"url": PAGE_URL, "status": 200, "title": "", "text": "", "screenshots": []}

    with pytest.raises(BrowserUnavailableError, match="no controls"):
        await build_client(responder(body)).observe(PAGE_URL)

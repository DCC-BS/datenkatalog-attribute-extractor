"""Tests for the web extractor's ordering, failure handling and warnings."""

from pathlib import Path

import httpx
import pytest

from datenkatalog_attribute_extractor.models.extraction import UploadSource, UrlSource
from datenkatalog_attribute_extractor.models.field import ExtractedField, PageExtraction
from datenkatalog_attribute_extractor.services.extractors.web_extractor import WebFieldExtractor
from datenkatalog_attribute_extractor.services.llm_health import LlmUnavailableError
from datenkatalog_attribute_extractor.services.web import url_policy
from datenkatalog_attribute_extractor.services.web.firecrawl_client import (
    FirecrawlClient,
    FirecrawlUnavailableError,
)
from datenkatalog_attribute_extractor.services.web.url_policy import UnsafeUrlError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FORM_URL = "https://example.org/anmeldung"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Resolve every host to a public address, so the URL policy is not the thing under test."""
    monkeypatch.setattr(url_policy, "_resolve", lambda host: ["93.184.216.34"])


class StubAgent:
    """Returns canned extractions, or raises, without touching an LLM."""

    def __init__(self, *, fields: list[str] | None = None, error: Exception | None = None) -> None:
        self._fields = fields if fields is not None else ["Familienname"]
        self._error = error
        self.listings: list[str] = []

    async def extract_listing(self, listing: str) -> PageExtraction:
        self.listings.append(listing)
        if self._error is not None:
            raise self._error
        return PageExtraction(fields=[ExtractedField(label=label) for label in self._fields])


class StubProbe:
    """Stands in for the LLM health probe."""

    def __init__(self, *, context: int | None = 250_000, error: Exception | None = None) -> None:
        self.served_context_tokens = context
        self.url = "http://llm:8000/health"
        self._error = error
        self.checked = False

    async def ensure_available(self) -> None:
        self.checked = True
        if self._error is not None:
            raise self._error


def build_client(html: str = "", *, status: int = 200, fail: bool = False) -> FirecrawlClient:
    """A Firecrawl client whose transport returns canned HTML."""

    def handle(request: httpx.Request) -> httpx.Response:
        if fail:
            raise httpx.ConnectError("Connection refused")
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "rawHtml": html or "<html><body><form></form></body></html>",
                    "metadata": {"statusCode": status, "sourceURL": FORM_URL, "title": "Anmeldung"},
                },
            },
        )

    return FirecrawlClient(
        scrape_url="http://fc:3002/v2/scrape", timeout_seconds=5, wait_ms=0, transport=httpx.MockTransport(handle)
    )


def build_extractor(
    *,
    html: str = "",
    agent: StubAgent | None = None,
    probe: StubProbe | None = None,
    client: FirecrawlClient | None = None,
    max_units: int = 30,
) -> tuple[WebFieldExtractor, StubAgent, StubProbe]:
    """Assemble an extractor over stubs."""
    chosen_agent = agent or StubAgent()
    chosen_probe = probe or StubProbe()
    extractor = WebFieldExtractor(
        chosen_agent,  # ty: ignore[invalid-argument-type]
        client or build_client(html),
        chosen_probe,  # ty: ignore[invalid-argument-type]
        max_units=max_units,
    )
    return extractor, chosen_agent, chosen_probe


async def drain(extractor: WebFieldExtractor, url: str = FORM_URL) -> list:
    """Consume every result the extractor yields."""
    return [result async for result in extractor.extract(UrlSource(url=url))]


def anmeldung_html() -> str:
    """The German questionnaire fixture."""
    return (FIXTURES / "anmeldung_form.html").read_text(encoding="utf-8")


async def test_supports_url_sources_only() -> None:
    extractor, _, _ = build_extractor()

    assert extractor.supports(UrlSource(url=FORM_URL)) is True
    assert extractor.supports(UploadSource(content=b"x", filename="a.pdf", media_type="application/pdf")) is False


async def test_a_form_page_yields_its_fields() -> None:
    extractor, agent, _ = build_extractor(html=anmeldung_html())

    results = await drain(extractor)

    assert len(results) == 1
    assert [field.label for field in results[0].fields] == ["Familienname"]
    assert "vater_familienname" in agent.listings[0]


async def test_an_unsafe_url_is_rejected_before_anything_is_scraped(monkeypatch) -> None:
    """The cheapest check runs first, and nothing else should have happened."""
    monkeypatch.setattr(url_policy, "_resolve", lambda host: ["127.0.0.1"])
    extractor, agent, probe = build_extractor()

    with pytest.raises(UnsafeUrlError):
        await drain(extractor, "https://sneaky.example.org/x")

    assert probe.checked is False
    assert agent.listings == []


async def test_the_llm_is_verified_before_the_page_is_scraped() -> None:
    """Scraping costs a browser render; a dead LLM makes it pointless."""
    probe = StubProbe(error=LlmUnavailableError("http://llm:8000/health", "Connection refused"))
    extractor, agent, _ = build_extractor(probe=probe)

    with pytest.raises(LlmUnavailableError):
        await drain(extractor)

    assert agent.listings == []


async def test_an_unreachable_firecrawl_aborts_the_run() -> None:
    extractor, agent, _ = build_extractor(client=build_client(fail=True))

    with pytest.raises(FirecrawlUnavailableError):
        await drain(extractor)

    assert agent.listings == []


async def test_a_page_with_no_controls_warns_rather_than_failing() -> None:
    extractor, _, _ = build_extractor(html="<html><body><p>Kein Formular</p></body></html>")

    results = await drain(extractor)

    assert results[0].fields == []
    assert any("No form controls" in warning for warning in results[0].warnings)


async def test_a_chunk_the_model_cannot_read_becomes_a_warning() -> None:
    """A page-local failure must not lose the rest of the run."""
    agent = StubAgent(error=ValueError("could not parse the response"))
    extractor, _, _ = build_extractor(html=anmeldung_html(), agent=agent)

    results = await drain(extractor)

    assert results[0].fields == []
    assert any("could not be processed" in warning for warning in results[0].warnings)


async def test_an_llm_that_dies_mid_run_aborts_rather_than_continuing() -> None:
    agent = StubAgent(error=httpx.ConnectError("Connection refused"))
    extractor, _, _ = build_extractor(html=anmeldung_html(), agent=agent)

    with pytest.raises(LlmUnavailableError):
        await drain(extractor)


async def test_a_multi_step_form_is_flagged() -> None:
    html = '<html><body><form><p>Schritt 1 von 3</p><label for="a">Name:</label><input id="a" name="a"></form></body></html>'
    extractor, _, _ = build_extractor(html=html)

    results = await drain(extractor)

    assert any("multi-step" in warning for warning in results[0].warnings)


async def test_an_ordinary_form_is_not_flagged_as_multi_step() -> None:
    extractor, _, _ = build_extractor(html=anmeldung_html())

    results = await drain(extractor)

    assert not any("multi-step" in warning for warning in results[0].warnings)


async def test_a_firecrawl_partial_scrape_warning_reaches_the_reviewer() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "rawHtml": anmeldung_html(),
                    "warning": "The engine used does not support the following features: screenshot",
                    "metadata": {"statusCode": 200, "sourceURL": FORM_URL, "title": "Anmeldung"},
                },
            },
        )

    client = FirecrawlClient(
        scrape_url="http://fc:3002/v2/scrape",
        timeout_seconds=5,
        wait_ms=0,
        transport=httpx.MockTransport(handle),
    )
    extractor, _, _ = build_extractor(client=client)

    results = await drain(extractor)

    assert any("partial scrape" in warning for warning in results[0].warnings)


def sprawling_html(sections: int = 60) -> str:
    """A form far too large for a small context, so it must be split."""
    blocks = []
    for index in range(sections):
        blocks.append(
            f"<h2>Abschnitt {index} mit einer ziemlich langen Überschrift</h2>"
            f'<label for="f{index}">Feld {index} mit einer ausführlichen Beschriftung:</label>'
            f'<input type="text" id="f{index}" name="feld_{index}">'
        )
    return f"<html><body><form>{''.join(blocks)}</form></body></html>"


async def test_a_page_too_large_for_the_context_is_split_into_several_results() -> None:
    extractor, agent, _ = build_extractor(html=sprawling_html(), probe=StubProbe(context=4096))

    results = await drain(extractor)

    assert len(results) > 1
    assert len(agent.listings) == len(results)
    assert [result.page for result in results] == list(range(1, len(results) + 1))


async def test_exceeding_the_unit_cap_warns_instead_of_silently_truncating() -> None:
    """Dropping work quietly is the failure mode this project keeps designing against."""
    extractor, _, _ = build_extractor(html=sprawling_html(), probe=StubProbe(context=4096), max_units=2)

    results = await drain(extractor)

    warnings = [warning for result in results for warning in result.warnings]
    assert len(results) == 2
    assert any("only the first 2" in warning for warning in warnings)


async def test_warnings_are_reported_once_and_not_per_chunk() -> None:
    extractor, _, _ = build_extractor(html=sprawling_html(), probe=StubProbe(context=4096), max_units=2)

    results = await drain(extractor)

    capped = [w for result in results for w in result.warnings if "only the first" in w]
    assert len(capped) == 1


async def test_a_non_url_source_is_a_type_error() -> None:
    extractor, _, _ = build_extractor()
    upload = UploadSource(content=b"x", filename="a.pdf", media_type="application/pdf")

    with pytest.raises(TypeError):
        [result async for result in extractor.extract(upload)]

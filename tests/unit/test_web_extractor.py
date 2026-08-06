"""Tests for the web extractor: which reading it picks, its ordering, failures and warnings.

The browser service is faked at the HTTP boundary with `httpx.MockTransport`, so these run
offline and describe the contract between the two: an observation of the page's controls plus
screenshots of the same render. What the browser makes of a real page is covered by
`tests/integration/test_observe_js.py`, which needs a browser and does not run in CI.
"""

import base64

import httpx
import pytest

from datenkatalog_attribute_extractor.models.extraction import UploadSource, UrlSource
from datenkatalog_attribute_extractor.models.field import ExtractedField, PageExtraction
from datenkatalog_attribute_extractor.services.extractors.web_extractor import WebFieldExtractor
from datenkatalog_attribute_extractor.services.llm_health import LlmUnavailableError
from datenkatalog_attribute_extractor.services.web import url_policy
from datenkatalog_attribute_extractor.services.web.browser_client import (
    BrowserClient,
    BrowserUnavailableError,
    PageUnreadableError,
)
from datenkatalog_attribute_extractor.services.web.url_policy import UnsafeUrlError

FORM_URL = "https://example.org/anmeldung"
OBSERVE_URL = "http://browser:3100/observe"

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n fake").decode()


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Resolve every host to a public address, so the URL policy is not the thing under test."""
    monkeypatch.setattr(url_policy, "_resolve", lambda host: ["93.184.216.34"])


def control(label: str, *, name: str = "", source: str = "markup", context: list[str] | None = None) -> dict:
    """One control as the browser service reports it."""
    return {
        "kind": "text",
        "label": label,
        "label_source": source if label else "",
        "context_path": context or ["Anmeldung"],
        "name": name,
        "options": [],
        "in_form": True,
    }


class StubAgent:
    """Returns canned extractions from a listing, without touching an LLM."""

    def __init__(self, *, fields: list[str] | None = None, error: Exception | None = None) -> None:
        self._fields = fields if fields is not None else ["Familienname"]
        self._error = error
        self.listings: list[str] = []

    async def extract_listing(self, listing: str) -> PageExtraction:
        self.listings.append(listing)
        if self._error is not None:
            raise self._error
        return PageExtraction(fields=[ExtractedField(label=label) for label in self._fields])


class StubVisionAgent:
    """Returns canned extractions from an image, without touching an LLM."""

    def __init__(self, *, fields: list[str] | None = None, error: Exception | None = None) -> None:
        self._fields = fields if fields is not None else ["Gesehenes Feld"]
        self._error = error
        self.images: list[bytes] = []

    async def extract_page(self, png_bytes: bytes) -> PageExtraction:
        self.images.append(png_bytes)
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


def build_client(
    controls: list[dict] | None = None,
    *,
    screenshots: int = 1,
    status: int = 200,
    text: str = "Anmeldung",
    fail: bool = False,
) -> BrowserClient:
    """A browser client whose transport returns a canned observation."""

    def handle(request: httpx.Request) -> httpx.Response:
        if fail:
            raise httpx.ConnectError("Connection refused")
        return httpx.Response(
            200,
            json={
                "url": FORM_URL,
                "status": status,
                "title": "Anmeldung",
                "text": text,
                "controls": controls if controls is not None else [control("Familienname:", name="familienname")],
                "screenshots": [PNG] * screenshots,
            },
        )

    return BrowserClient(
        observe_url=OBSERVE_URL,
        timeout_seconds=5,
        wait_ms=0,
        max_tiles=30,
        transport=httpx.MockTransport(handle),
    )


def build_extractor(
    *,
    controls: list[dict] | None = None,
    agent: StubAgent | None = None,
    vision_agent: StubVisionAgent | None = None,
    probe: StubProbe | None = None,
    client: BrowserClient | None = None,
    max_units: int = 30,
    min_labelled_share: float = 0.5,
) -> tuple[WebFieldExtractor, StubAgent, StubVisionAgent, StubProbe]:
    """Assemble an extractor over stubs."""
    chosen_agent = agent or StubAgent()
    chosen_vision = vision_agent or StubVisionAgent()
    chosen_probe = probe or StubProbe()
    extractor = WebFieldExtractor(
        chosen_agent,  # ty: ignore[invalid-argument-type]
        chosen_vision,  # ty: ignore[invalid-argument-type]
        client or build_client(controls),
        chosen_probe,  # ty: ignore[invalid-argument-type]
        max_units=max_units,
        min_labelled_share=min_labelled_share,
    )
    return extractor, chosen_agent, chosen_vision, chosen_probe


async def drain(extractor: WebFieldExtractor, url: str = FORM_URL) -> list:
    """Consume every result the extractor yields."""
    return [result async for result in extractor.extract(UrlSource(url=url))]


async def test_supports_url_sources_only() -> None:
    extractor, *_ = build_extractor()

    assert extractor.supports(UrlSource(url=FORM_URL)) is True
    assert extractor.supports(UploadSource(content=b"x", filename="a.pdf", media_type="application/pdf")) is False


async def test_a_labelled_page_is_read_from_its_controls() -> None:
    extractor, agent, vision, _ = build_extractor()

    results = await drain(extractor)

    assert len(results) == 1
    assert [field.label for field in results[0].fields] == ["Familienname"]
    assert "Familienname:" in agent.listings[0]
    assert vision.images == []


async def test_a_page_whose_controls_are_mostly_unlabelled_is_read_from_its_screenshots() -> None:
    """The measurement that decides this is the point: an inventory of nameless boxes is not an answer."""
    controls = [control(""), control(""), control(""), control("Nur dieses Feld")]
    extractor, agent, vision, _ = build_extractor(controls=controls, client=build_client(controls, screenshots=2))

    results = await drain(extractor)

    assert agent.listings == []
    assert len(vision.images) == 2
    assert [field.label for field in results[0].fields] == ["Gesehenes Feld"]
    assert any("read from screenshots" in warning for warning in results[0].warnings)


async def test_the_screenshot_reading_can_be_demanded_outright() -> None:
    """The automatic choice is a measurement, not a certainty; a reviewer can overrule it."""
    extractor, agent, vision, _ = build_extractor(client=build_client(screenshots=2))

    results = [result async for result in extractor.extract(UrlSource(url=FORM_URL, force_screenshots=True))]

    assert agent.listings == []
    assert len(vision.images) == 2
    assert any("as requested" in warning for warning in results[0].warnings)


async def test_a_well_labelled_page_is_read_from_its_controls_unless_asked_otherwise() -> None:
    extractor, agent, vision, _ = build_extractor(client=build_client(screenshots=2))

    results = [result async for result in extractor.extract(UrlSource(url=FORM_URL, force_screenshots=False))]

    assert vision.images == []
    assert len(agent.listings) == 1
    assert results[0].fields


async def test_a_page_with_no_controls_at_all_is_read_from_its_screenshots() -> None:
    """A form drawn on a canvas has no controls to find and is still a form."""
    extractor, _, vision, _ = build_extractor(client=build_client([], screenshots=1))

    results = await drain(extractor)

    assert len(vision.images) == 1
    assert any("No form controls could be read" in warning for warning in results[0].warnings)


async def test_a_page_with_neither_controls_nor_screenshots_warns_rather_than_failing() -> None:
    extractor, _, _, _ = build_extractor(client=build_client([], screenshots=0))

    results = await drain(extractor)

    assert results[0].fields == []
    assert any("No form controls were found" in warning for warning in results[0].warnings)


async def test_each_screenshot_tile_is_its_own_result() -> None:
    vision = StubVisionAgent()
    seen = {"n": 0}

    async def one_new_field_per_tile(png_bytes: bytes) -> PageExtraction:
        seen["n"] += 1
        return PageExtraction(fields=[ExtractedField(label=f"Feld {seen['n']}")])

    vision.extract_page = one_new_field_per_tile  # ty: ignore[invalid-assignment]
    extractor, _, _, _ = build_extractor(vision_agent=vision, client=build_client([], screenshots=3))

    results = await drain(extractor)

    assert [result.page for result in results] == [1, 2, 3]
    assert all(result.total_pages == 3 for result in results)
    assert [field.page for result in results for field in result.fields] == [1, 2, 3]


async def test_each_screenshot_result_carries_the_tile_it_was_read_from() -> None:
    """The reviewer's only picture of what the model saw: the live page is a fresh render."""
    extractor, _, _, _ = build_extractor(client=build_client([], screenshots=2))

    results = await drain(extractor)

    assert [result.image for result in results] == [base64.b64decode(PNG)] * 2


async def test_a_listing_result_carries_no_picture() -> None:
    """A control listing was never a picture, so there is nothing to show beside it."""
    extractor, _, _, _ = build_extractor()

    results = await drain(extractor)

    assert results[0].image is None


async def test_a_field_seen_on_two_adjoining_screens_is_reported_once() -> None:
    """Tiles overlap so nothing is cut in half, which means the overlap is read twice."""
    extractor, _, _, _ = build_extractor(client=build_client([], screenshots=3))

    results = await drain(extractor)

    assert [field.label for result in results for field in result.fields] == ["Gesehenes Feld"]
    assert any("reported once" in warning for result in results for warning in result.warnings)


async def test_the_same_label_far_apart_is_not_merged() -> None:
    """Only adjoining screens are compared, so a repeat three screens later survives."""
    vision = StubVisionAgent(fields=["Datum"])
    vision_calls = {"n": 0}

    async def alternating(png_bytes: bytes) -> PageExtraction:
        vision_calls["n"] += 1
        label = "Datum" if vision_calls["n"] % 2 else "Anderes"
        return PageExtraction(fields=[ExtractedField(label=label)])

    vision.extract_page = alternating  # ty: ignore[invalid-assignment]
    extractor, _, _, _ = build_extractor(vision_agent=vision, client=build_client([], screenshots=3))

    results = await drain(extractor)

    assert [field.label for result in results for field in result.fields] == ["Datum", "Anderes", "Datum"]


async def test_an_unsafe_url_is_rejected_before_anything_is_rendered(monkeypatch) -> None:
    """The cheapest check runs first, and nothing else should have happened."""
    monkeypatch.setattr(url_policy, "_resolve", lambda host: ["127.0.0.1"])
    extractor, agent, _, probe = build_extractor()

    with pytest.raises(UnsafeUrlError):
        await drain(extractor, "https://sneaky.example.org/x")

    assert probe.checked is False
    assert agent.listings == []


async def test_the_llm_is_verified_before_the_page_is_rendered() -> None:
    """A render costs a browser and several seconds; a dead LLM makes it pointless."""
    probe = StubProbe(error=LlmUnavailableError("http://llm:8000/health", "Connection refused"))
    extractor, agent, _, _ = build_extractor(probe=probe)

    with pytest.raises(LlmUnavailableError):
        await drain(extractor)

    assert agent.listings == []


async def test_an_unreachable_browser_aborts_the_run() -> None:
    extractor, agent, _, _ = build_extractor(client=build_client(fail=True))

    with pytest.raises(BrowserUnavailableError):
        await drain(extractor)

    assert agent.listings == []


async def test_a_page_that_answered_an_error_status_is_the_callers_problem() -> None:
    extractor, _, _, _ = build_extractor(client=build_client(status=404))

    with pytest.raises(PageUnreadableError, match="404"):
        await drain(extractor)


async def test_a_chunk_the_model_cannot_read_becomes_a_warning() -> None:
    """A page-local failure must not lose the rest of the run."""
    agent = StubAgent(error=ValueError("could not parse the response"))
    extractor, _, _, _ = build_extractor(agent=agent)

    results = await drain(extractor)

    assert results[0].fields == []
    assert any("could not be processed" in warning for warning in results[0].warnings)


async def test_a_screenshot_the_model_cannot_read_becomes_a_warning() -> None:
    vision = StubVisionAgent(error=ValueError("could not parse the response"))
    extractor, _, _, _ = build_extractor(vision_agent=vision, client=build_client([], screenshots=1))

    results = await drain(extractor)

    assert results[0].fields == []
    assert any("could not be processed" in warning for warning in results[0].warnings)


async def test_an_llm_that_dies_mid_run_aborts_rather_than_continuing() -> None:
    agent = StubAgent(error=httpx.ConnectError("Connection refused"))
    extractor, _, _, _ = build_extractor(agent=agent)

    with pytest.raises(LlmUnavailableError):
        await drain(extractor)


async def test_a_multi_step_form_is_flagged() -> None:
    client = build_client(text="Anmeldung Schritt 1 von 3 Weiter")
    extractor, _, _, _ = build_extractor(client=client)

    results = await drain(extractor)

    assert any("multi-step" in warning for warning in results[0].warnings)


async def test_an_ordinary_form_is_not_flagged_as_multi_step() -> None:
    extractor, _, _, _ = build_extractor(client=build_client(text="Weitere Angaben zur Person"))

    results = await drain(extractor)

    assert not any("multi-step" in warning for warning in results[0].warnings)


def sprawling_controls(sections: int = 60) -> list[dict]:
    """A form far too large for a small context, so its listing must be split."""
    return [
        control(
            f"Feld {index} mit einer ausführlichen Beschriftung:",
            name=f"feld_{index}",
            context=[f"Abschnitt {index} mit einer ziemlich langen Überschrift"],
        )
        for index in range(sections)
    ]


async def test_a_page_too_large_for_the_context_is_split_into_several_results() -> None:
    extractor, agent, _, _ = build_extractor(controls=sprawling_controls(), probe=StubProbe(context=4096))

    results = await drain(extractor)

    assert len(results) > 1
    assert len(agent.listings) == len(results)
    assert [result.page for result in results] == list(range(1, len(results) + 1))


async def test_exceeding_the_unit_cap_warns_instead_of_silently_truncating() -> None:
    """Dropping work quietly is the failure mode this project keeps designing against."""
    extractor, _, _, _ = build_extractor(controls=sprawling_controls(), probe=StubProbe(context=4096), max_units=2)

    results = await drain(extractor)

    warnings = [warning for result in results for warning in result.warnings]
    assert len(results) == 2
    assert any("only the first 2" in warning for warning in warnings)


async def test_exceeding_the_unit_cap_on_screenshots_warns_too() -> None:
    extractor, _, vision, _ = build_extractor(client=build_client([], screenshots=5), max_units=2)

    results = await drain(extractor)

    warnings = [warning for result in results for warning in result.warnings]
    assert len(vision.images) == 2
    assert any("only the first 2" in warning for warning in warnings)


async def test_warnings_are_reported_once_and_not_per_chunk() -> None:
    extractor, _, _, _ = build_extractor(controls=sprawling_controls(), probe=StubProbe(context=4096), max_units=2)

    results = await drain(extractor)

    capped = [w for result in results for w in result.warnings if "only the first" in w]
    assert len(capped) == 1


async def test_a_non_url_source_is_a_type_error() -> None:
    extractor, *_ = build_extractor()
    upload = UploadSource(content=b"x", filename="a.pdf", media_type="application/pdf")

    with pytest.raises(TypeError):
        [result async for result in extractor.extract(upload)]

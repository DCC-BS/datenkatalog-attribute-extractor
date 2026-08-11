"""Tests for the web extractor: what it walks, what it reads, and what it admits to missing.

The browser service is faked at the HTTP boundary with `httpx.MockTransport`, so these run
offline and describe the contract between the two: a form in steps, each step in screens, each
screen a picture plus the controls standing on it. What the browser makes of a real page is
covered by `tests/integration/test_observe_js.py`, which needs a browser and does not run in CI.
"""

import base64
import json

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


def control(label: str, *, kind: str = "text", context: list[str] | None = None) -> dict:
    """One control as the browser service reports it."""
    return {
        "kind": kind,
        "label": label,
        "label_source": "markup" if label else "",
        "context_path": context or ["Anmeldung"],
        "name": "",
        "options": [],
        "in_form": True,
    }


def step(
    index: int,
    *,
    controls: list[dict] | None = None,
    tiles: int = 1,
    label: str = "",
    blocked_by: list[str] | None = None,
) -> dict:
    """One step of a form as the browser service reports it.

    Every control is placed on every tile, which is the ordinary case for a step that fits on
    one screen and harmless for the tests that use several.
    """
    entries = controls if controls is not None else [control("Familienname")]
    return {
        "index": index,
        "label": label,
        "title": "Anmeldung",
        "controls": entries,
        "tiles": [{"image": PNG, "control_indices": list(range(len(entries)))} for _ in range(tiles)],
        "filled": 0,
        "advanced_by": "Weiter",
        "blocked_by": blocked_by or [],
    }


class StubAgent:
    """Returns canned extractions from a run of screens, without touching an LLM."""

    def __init__(self, *, fields: list[str] | None = None, error: Exception | None = None) -> None:
        self._fields = fields if fields is not None else ["Gesehenes Feld"]
        self._error = error
        self.calls: list[list[bytes]] = []
        self.hints: list[str] = []

    @property
    def images(self) -> list[bytes]:
        """Every screen the agent was shown, across all calls."""
        return [image for call in self.calls for image in call]

    async def extract_screens(self, screens: list[bytes], hints: str = "") -> PageExtraction:
        self.calls.append(list(screens))
        self.hints.append(hints)
        if self._error is not None:
            raise self._error
        return PageExtraction(fields=[ExtractedField(label=label) for label in self._fields])


class StubProbe:
    """Stands in for the LLM health probe."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.served_context_tokens = 250_000
        self.url = "http://llm:8000/health"
        self._error = error
        self.checked = False

    async def ensure_available(self) -> None:
        self.checked = True
        if self._error is not None:
            raise self._error


def build_client(
    steps: list[dict] | None = None,
    *,
    status: int = 200,
    stopped: str = "no_next",
    fail: bool = False,
) -> tuple[BrowserClient, list[dict]]:
    """A browser client whose transport returns a canned walk, and the payloads it was sent."""
    sent: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if fail:
            raise httpx.ConnectError("Connection refused")
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "url": FORM_URL,
                "status": status,
                "title": "Anmeldung",
                "steps": steps if steps is not None else [step(1)],
                "stopped_because": stopped,
            },
        )

    client = BrowserClient(
        observe_url=OBSERVE_URL,
        timeout_seconds=5,
        wait_ms=0,
        max_tiles=30,
        step_wait_ms=0,
        transport=httpx.MockTransport(handle),
    )
    return client, sent


def build_extractor(
    *,
    steps: list[dict] | None = None,
    agent: StubAgent | None = None,
    probe: StubProbe | None = None,
    client: BrowserClient | None = None,
    max_units: int = 30,
    max_steps: int = 10,
    screens_per_call: int = 1,
) -> tuple[WebFieldExtractor, StubAgent, StubProbe]:
    """Assemble an extractor over stubs.

    One screen per call by default, so a test about anything else does not have to think about
    grouping; the grouping itself is what the tests naming it set it for.
    """
    chosen_agent = agent or StubAgent()
    chosen_probe = probe or StubProbe()
    extractor = WebFieldExtractor(
        chosen_agent,  # ty: ignore[invalid-argument-type]
        client or build_client(steps)[0],
        chosen_probe,  # ty: ignore[invalid-argument-type]
        max_units=max_units,
        max_steps=max_steps,
        screens_per_call=screens_per_call,
    )
    return extractor, chosen_agent, chosen_probe


async def drain(extractor: WebFieldExtractor, url: str = FORM_URL, **kwargs) -> list:
    """Consume every result the extractor yields."""
    return [result async for result in extractor.extract(UrlSource(url=url, **kwargs))]


async def test_supports_url_sources_only() -> None:
    extractor, *_ = build_extractor()

    assert extractor.supports(UrlSource(url=FORM_URL)) is True
    assert extractor.supports(UploadSource(content=b"x", filename="a.pdf", media_type="application/pdf")) is False


async def test_every_screen_is_read_from_its_picture() -> None:
    """The picture is the reading; there is no markup-only path left to fall back to."""
    extractor, agent, _ = build_extractor(steps=[step(1, tiles=2)])

    results = await drain(extractor)

    assert len(agent.images) == 2
    assert [result.page for result in results] == [1, 2]
    assert all(result.total_pages == 2 for result in results)


async def test_the_labels_of_a_screen_ride_along_with_its_picture() -> None:
    """The page's own spelling, as a reference beside the image rather than instead of it."""
    controls = [control("AHV-Nummer"), control("Grösse")]
    extractor, agent, _ = build_extractor(steps=[step(1, controls=controls)])

    await drain(extractor)

    assert "AHV-Nummer" in agent.hints[0]
    assert "Grösse" in agent.hints[0]


async def test_a_screen_with_nothing_to_spell_sends_no_reference() -> None:
    extractor, agent, _ = build_extractor(steps=[step(1, controls=[control("")])])

    await drain(extractor)

    assert agent.hints == [""]


async def test_every_result_carries_the_pictures_it_was_read_from() -> None:
    """The reviewer's only sight of what the model saw: the live form is a fresh render."""
    extractor, _, _ = build_extractor(steps=[step(1, tiles=2)], screens_per_call=2)

    results = await drain(extractor)

    assert [result.images for result in results] == [[base64.b64decode(PNG)] * 2]


async def test_consecutive_screens_of_a_step_go_into_one_call() -> None:
    """A field split by a screen boundary is two halves to a model shown one of them."""
    extractor, agent, _ = build_extractor(steps=[step(1, tiles=5)], screens_per_call=4)

    results = await drain(extractor)

    assert [len(call) for call in agent.calls] == [4, 1]
    assert [result.page for result in results] == [1, 2]


async def test_screens_are_never_grouped_across_a_step() -> None:
    """Two steps are two pictures of the form; nothing at the end of one continues the next."""
    extractor, agent, _ = build_extractor(steps=[step(1, tiles=2), step(2, tiles=1)], screens_per_call=4)

    await drain(extractor)

    assert [len(call) for call in agent.calls] == [2, 1]


async def test_the_labels_of_every_screen_in_a_call_are_offered_together() -> None:
    extractor, agent, _ = build_extractor(
        steps=[
            {
                **step(1, controls=[control("Vorname"), control("Nachname")]),
                "tiles": [
                    {"image": PNG, "control_indices": [0]},
                    {"image": PNG, "control_indices": [1]},
                ],
            }
        ],
        screens_per_call=2,
    )

    await drain(extractor)

    assert len(agent.hints) == 1
    assert "Vorname" in agent.hints[0]
    assert "Nachname" in agent.hints[0]


async def test_a_form_in_steps_is_walked_and_every_step_read() -> None:
    """The whole point: the first step of a cantonal wizard is a fraction of the fields."""
    steps = [
        step(1, controls=[control("Meldung betrifft")]),
        step(2, controls=[control("Vorname")]),
        step(3, controls=[control("Datum")]),
    ]
    agent = StubAgent(fields=["Feld"])
    extractor, agent, _ = build_extractor(steps=steps, agent=agent)

    results = await drain(extractor)

    assert len(agent.images) == 3
    assert any("followed through 3 steps" in warning for warning in results[0].warnings)


async def test_the_walk_is_asked_for_only_where_it_is_wanted() -> None:
    client, sent = build_client([step(1)])
    extractor, _, _ = build_extractor(client=client, max_steps=7)

    await drain(extractor)
    await drain(extractor, follow_steps=False)

    assert [payload["maxSteps"] for payload in sent] == [7, 1]


async def test_reading_only_the_first_step_says_so() -> None:
    extractor, _, _ = build_extractor(steps=[step(1)])

    results = await drain(extractor, follow_steps=False)

    assert any("Only the first step" in warning for warning in results[0].warnings)


async def test_a_step_that_would_not_let_its_next_button_be_pressed_says_so() -> None:
    """A greyed-out Weiter is not the end of the form, and must not be reported as one."""
    stuck = step(1, blocked_by=["Strasse", "Beginn"])
    extractor, _, _ = build_extractor(client=build_client([stuck], stopped="blocked")[0])

    results = await drain(extractor)

    assert any("would not let its next button be pressed" in warning for warning in results[0].warnings)
    assert any("would not accept: Strasse, Beginn" in warning for warning in results[0].warnings)


async def test_a_form_that_refused_to_advance_says_the_inventory_is_partial() -> None:
    """A wizard that will not take the placeholder answers yields a partial list either way.

    Saying nothing would be the worst outcome available: a fraction of the fields, looking
    exactly like the whole form.
    """
    extractor, _, _ = build_extractor(steps=[step(1)], client=build_client([step(1)], stopped="unchanged")[0])

    results = await drain(extractor)

    assert any("would not advance" in warning for warning in results[0].warnings)


async def test_a_field_seen_on_two_adjoining_screens_is_reported_once() -> None:
    """Tiles overlap so nothing is cut in half, which means the overlap is read twice."""
    extractor, _, _ = build_extractor(steps=[step(1, tiles=3)])

    results = await drain(extractor)

    assert [field.label for result in results for field in result.fields] == ["Gesehenes Feld"]
    assert any("already been reported" in warning for result in results for warning in result.warnings)


async def test_the_same_label_far_apart_within_a_step_is_not_merged() -> None:
    """Within a step only adjoining screens are compared, so a repeat two screens later stands."""
    calls = {"n": 0}

    async def alternating(screens: list[bytes], hints: str = "") -> PageExtraction:
        calls["n"] += 1
        label = "Datum" if calls["n"] % 2 else "Anderes"
        return PageExtraction(fields=[ExtractedField(label=label, context_path=[f"Abschnitt {calls['n']}"])])

    agent = StubAgent()
    agent.extract_screens = alternating  # ty: ignore[invalid-assignment]
    extractor, _, _ = build_extractor(agent=agent, steps=[step(1, tiles=3)])

    results = await drain(extractor)

    assert [field.label for result in results for field in result.fields] == ["Datum", "Anderes", "Datum"]


async def test_a_field_repeated_by_a_later_step_is_reported_once() -> None:
    """A form service commonly adds each step to the page rather than replacing it."""
    extractor, _, _ = build_extractor(steps=[step(1), step(2), step(3)])

    results = await drain(extractor)

    assert [field.label for result in results for field in result.fields] == ["Gesehenes Feld"]


async def test_the_same_label_under_different_headings_survives_across_steps() -> None:
    """`Vorname` under *Meldende Person* and under *Betroffene Person* are two fields."""
    calls = {"n": 0}

    async def per_step(screens: list[bytes], hints: str = "") -> PageExtraction:
        calls["n"] += 1
        return PageExtraction(fields=[ExtractedField(label="Vorname", context_path=[f"Person {calls['n']}"])])

    agent = StubAgent()
    agent.extract_screens = per_step  # ty: ignore[invalid-assignment]
    extractor, _, _ = build_extractor(agent=agent, steps=[step(1), step(2)])

    results = await drain(extractor)

    assert [field.context_path for result in results for field in result.fields] == [["Person 1"], ["Person 2"]]


async def test_more_screens_than_allowed_are_cut_and_reported() -> None:
    extractor, agent, _ = build_extractor(steps=[step(1, tiles=5)], max_units=2)

    results = await drain(extractor)

    assert len(agent.images) == 2
    assert any("only the first 2 were processed" in warning for warning in results[0].warnings)


async def test_a_form_that_could_not_be_photographed_warns_rather_than_failing() -> None:
    extractor, _, _ = build_extractor(steps=[step(1, tiles=0)])

    results = await drain(extractor)

    assert results[0].fields == []
    assert any("Nothing could be photographed" in warning for warning in results[0].warnings)


async def test_an_unsafe_url_is_rejected_before_anything_is_rendered(monkeypatch) -> None:
    """The cheapest check runs first, and nothing else should have happened."""
    monkeypatch.setattr(url_policy, "_resolve", lambda host: ["127.0.0.1"])
    extractor, agent, probe = build_extractor()

    with pytest.raises(UnsafeUrlError):
        await drain(extractor, "https://sneaky.example.org/x")

    assert probe.checked is False
    assert agent.images == []


async def test_the_llm_is_verified_before_the_form_is_rendered() -> None:
    """A walk costs a browser and minutes; a dead LLM makes it pointless."""
    probe = StubProbe(error=LlmUnavailableError("http://llm:8000/health", "Connection refused"))
    extractor, agent, _ = build_extractor(probe=probe)

    with pytest.raises(LlmUnavailableError):
        await drain(extractor)

    assert agent.images == []


async def test_an_unreachable_browser_aborts_the_run() -> None:
    extractor, agent, _ = build_extractor(client=build_client(fail=True)[0])

    with pytest.raises(BrowserUnavailableError):
        await drain(extractor)

    assert agent.images == []


async def test_a_page_that_answered_an_error_status_is_the_callers_problem() -> None:
    extractor, _, _ = build_extractor(client=build_client([step(1)], status=404)[0])

    with pytest.raises(PageUnreadableError, match="404"):
        await drain(extractor)


async def test_a_screen_the_model_cannot_read_becomes_a_warning() -> None:
    """A screen-local failure must not lose the rest of the run."""
    agent = StubAgent(error=ValueError("could not parse the response"))
    extractor, _, _ = build_extractor(agent=agent, steps=[step(1)])

    results = await drain(extractor)

    assert results[0].fields == []
    assert any("could not be processed" in warning for warning in results[0].warnings)


async def test_an_llm_that_dies_mid_run_aborts_rather_than_continuing() -> None:
    agent = StubAgent(error=httpx.ConnectError("Connection refused"))
    extractor, _, _ = build_extractor(agent=agent, steps=[step(1)])

    with pytest.raises(LlmUnavailableError):
        await drain(extractor)

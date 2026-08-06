"""What a real browser makes of a real page, against the running browser service.

Excluded from CI: it needs `make docker-up-web`. Run locally with

    PYTHONPATH=src uv run --env-file .env python -m pytest tests/integration/test_observe_js.py -v

The unit tests work from saved observations, which pins down everything downstream of the
render but nothing about the render itself. This is where `docker/browser/observe.js` is
actually exercised — and where the fixtures those unit tests use come from, so a change to the
collector that alters them shows up here first.

The pages are chosen to disagree with each other: one states every association in its markup,
one is a documentation site with a form buried in navigation, one states nothing at all.
"""

import json
import os
from pathlib import Path

import httpx
import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
BROWSER_URL = os.getenv("BROWSER_API_URL", "http://localhost:3100")

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_BROWSER_TESTS") is None,
    reason="set RUN_BROWSER_TESTS=1 and start the browser service (make docker-up-web) to run these",
)


def observe(**payload) -> dict:
    """Ask the browser service to render and observe something."""
    with httpx.Client(timeout=180) as client:
        response = client.post(f"{BROWSER_URL}/observe", json={"screenshots": False} | payload)
        response.raise_for_status()
        return response.json()


def observe_fixture(name: str) -> dict:
    """Render a saved page and observe it."""
    return observe(html=(FIXTURES / f"{name}.html").read_text(encoding="utf-8"), waitMs=1500)


def saved(name: str) -> dict:
    """The observation committed for this page."""
    return json.loads((FIXTURES / f"{name}.observation.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["anmeldung_form", "w3schools_forms"])
def test_the_saved_observations_still_describe_these_pages(name: str) -> None:
    """The unit tests are only worth anything while their fixtures match what the browser does."""
    observed = observe_fixture(name)

    assert [control["label"] for control in observed["controls"]] == [
        control["label"] for control in saved(name)["controls"]
    ]


def test_markup_that_states_its_labels_is_believed() -> None:
    controls = observe_fixture("anmeldung_form")["controls"]

    by_name = {control["name"]: control for control in controls}
    assert by_name["schueler_familienname"]["label"] == "Familienname:"
    assert by_name["schueler_familienname"]["label_source"] == "markup"
    assert by_name["groesse"]["label"] == "Grösse (in cm)"


def test_a_page_that_states_nothing_is_read_from_its_layout() -> None:
    """The Vaadin form: no `name`, no `<label>`, no heading tag, no `<form>` around a field."""
    controls = observe_fixture("zetcom_vaadin_form")["controls"]

    labels = [control["label"] for control in controls]
    assert "Organisation*" in labels
    assert "PLZ*" in labels
    assert all(control["label_source"] in {"layout", ""} for control in controls)


def test_site_navigation_does_not_become_a_heading() -> None:
    """A menu item is styled exactly like a section title; what separates them is being a link."""
    controls = observe_fixture("w3schools_forms")["controls"]

    headings = {heading for control in controls for heading in control["context_path"]}
    assert "HTML Forms" in headings
    assert not any("Breadcrumb" in heading or "Tutorials" in heading for heading in headings)


def test_a_caption_split_by_a_link_is_read_as_one_sentence() -> None:
    html = """
    <body style="font: 16px sans-serif">
      <div><span>Ich habe die <a href="#">Wegleitung</a> gelesen *</span>
      <input type="checkbox" style="margin-left: 8px"></div>
    </body>
    """
    controls = observe(html=html, waitMs=100)["controls"]

    assert [control["label"] for control in controls] == ["Ich habe die Wegleitung gelesen *"]


def test_a_label_is_not_read_across_another_field() -> None:
    """Two columns of label and field: what disqualifies the far label is the near field."""
    html = """
    <body style="font: 16px sans-serif">
      <table><tr>
        <td>Vorname</td><td><input id="a"></td>
        <td>Nachname</td><td><input id="b"></td>
      </tr></table>
    </body>
    """
    controls = observe(html=html, waitMs=100)["controls"]

    assert [control["label"] for control in controls] == ["Vorname", "Nachname"]


def test_a_caption_above_its_field_is_found() -> None:
    """The other common layout, and the one floating-label frameworks produce."""
    html = """
    <body style="font: 16px sans-serif">
      <div style="width: 300px">E-Mail-Adresse<br><input id="a" style="width: 280px"></div>
    </body>
    """
    controls = observe(html=html, waitMs=100)["controls"]

    assert [control["label"] for control in controls] == ["E-Mail-Adresse"]


def test_a_control_that_is_not_an_input_is_still_a_field() -> None:
    """A grid the applicant fills in row by row holds no `<input>` until a cell is opened."""
    html = """
    <body style="font: 16px sans-serif">
      <div>Beilagen*
        <div role="grid" style="width: 400px; height: 100px; display: inline-block">
          <div role="row"><div role="gridcell">Budget</div></div>
          <div role="row"><div role="gridcell">Offerte</div></div>
        </div>
      </div>
    </body>
    """
    controls = observe(html=html, waitMs=100)["controls"]

    assert len(controls) == 1
    assert controls[0]["kind"] == "tabelle"
    assert controls[0]["label"] == "Beilagen*"
    assert controls[0]["options"] == ["Budget", "Offerte"]


def test_a_form_inside_an_iframe_is_found() -> None:
    """Embedded form providers put the whole form in a frame, where a page query finds nothing."""
    inner = "<label for='a'>Name des Vereins</label><input id='a'>"
    html = f'<body><h1>Anmeldung</h1><iframe srcdoc="{inner}" width="600" height="200"></iframe></body>'

    controls = observe(html=html, waitMs=300)["controls"]

    assert [control["label"] for control in controls] == ["Name des Vereins"]


def test_a_hidden_section_is_not_a_field() -> None:
    """A collapsed part of a form is not a field until it is opened."""
    html = '<body><div style="display:none"><label for="a">Versteckt</label><input id="a"></div></body>'

    assert observe(html=html, waitMs=100)["controls"] == []


def test_screenshots_are_produced_for_the_page() -> None:
    """The vision fallback has nothing to read without these."""
    html = "<body style='height: 3000px'><input id='a'></body>"

    tiles = observe(html=html, waitMs=100, screenshots=True)["screenshots"]

    assert len(tiles) >= 2

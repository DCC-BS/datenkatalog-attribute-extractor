"""Tests for assembling observed controls into the listing the model reads.

These run against saved observations — what the browser reported for a page, captured once and
committed. They are deterministic, need neither browser nor network, and pin down the part
that is ours: grouping, the single-member group rule, and how the listing reads.

The three fixtures are deliberately different kinds of page. `anmeldung_form` states every
association in its markup, `w3schools_forms` is a real documentation page with a form buried in
site furniture, and `zetcom_vaadin_form` states nothing at all — no `name`, no `<label>`, no
heading tag, no `<form>` around a single field — so everything in it was read from the layout.
"""

import json
from pathlib import Path

import pytest

from datenkatalog_attribute_extractor.services.web.browser_client import ObservedControl
from datenkatalog_attribute_extractor.services.web.controls import (
    build_controls,
    form_marker_is_informative,
    render_listing,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def load(name: str) -> list[ObservedControl]:
    """The controls of a saved observation."""
    body = json.loads((FIXTURES / f"{name}.observation.json").read_text(encoding="utf-8"))
    return [ObservedControl(**entry) for entry in body["controls"]]


@pytest.fixture(scope="module")
def anmeldung():
    """The questionnaire whose markup states everything."""
    return build_controls(load("anmeldung_form"))


@pytest.fixture(scope="module")
def vaadin():
    """The generated form whose markup states nothing."""
    return build_controls(load("zetcom_vaadin_form"))


@pytest.fixture(scope="module")
def w3schools():
    """A real page with a form inside a site."""
    return build_controls(load("w3schools_forms"))


def labels(controls) -> list[str]:
    """The labels of the given controls, in order."""
    return [control.label for control in controls]


def find(controls, name: str):
    """The control with this `name` attribute."""
    return next(control for control in controls if control.name == name)


def test_related_radio_buttons_collapse_into_one_field(anmeldung) -> None:
    """Four radio buttons, one field — the rule the specification already sets out."""
    group = find(anmeldung, "berechtigt")

    assert group.kind == "radio-gruppe"
    assert group.label == "Weitere Angaben"
    assert group.options == ["beide Eltern", "nur Mutter", "nur Vater", "andere"]
    assert len([control for control in anmeldung if control.name == "berechtigt"]) == 1


def test_a_standalone_tick_box_keeps_its_own_caption(anmeldung) -> None:
    """A lone tick box is not a group, so it is named by its caption, not by its heading."""
    agb = find(anmeldung, "agb")

    assert agb.kind == "checkbox"
    assert agb.label == "Ich akzeptiere die Bedingungen"
    assert agb.options == []


def test_tick_boxes_that_share_nothing_stay_separate(w3schools) -> None:
    """Three boxes, three names, three fields — grouping is by `name`, not by proximity."""
    assert "I have a bike" in labels(w3schools)
    assert "I have a car" in labels(w3schools)
    assert "I have a boat" in labels(w3schools)


def test_repeated_labels_are_kept_apart_by_their_context(anmeldung) -> None:
    """The naming pass disambiguates from `context_path`, so it has to survive this far."""
    vater = find(anmeldung, "vater_familienname")
    mutter = find(anmeldung, "mutter_familienname")

    assert vater.label == mutter.label == "Familienname:"
    assert vater.context_path == mutter.context_path == ["Anmeldung Gymnasium", "Gesetzliche Vertreter"]


def test_a_page_that_states_nothing_still_yields_labelled_fields(vaadin) -> None:
    """Every one of these came from the rendered layout; the markup names none of them."""
    found = labels(vaadin)

    assert "Organisation*" in found
    assert "Telefon tagsüber*" in found
    assert "Ich habe die Wegleitung gelesen und verstanden *" in found
    assert "Angeschlossen bei Verband*" in found


def test_a_grid_is_one_field_with_its_rows_as_options(vaadin) -> None:
    beilagen = next(control for control in vaadin if control.label == "Beilagen*")

    assert beilagen.kind == "tabelle"
    assert beilagen.options == [
        "Budget/detaillierte Aufstellung Aufwand und Ertrag",
        "Offerte/n",
        "Finanzierungsnachweis",
    ]


def test_the_form_marker_is_dropped_where_it_would_condemn_the_whole_form(vaadin) -> None:
    """One `<form>` around an upload widget, every real field outside it."""
    assert form_marker_is_informative(vaadin) is False
    assert "ausserhalb" not in render_listing(vaadin)


def test_the_form_marker_survives_where_it_discriminates(anmeldung) -> None:
    assert form_marker_is_informative(anmeldung) is True
    assert "[ausserhalb eines <form>]" in render_listing(anmeldung)


def test_listing_renders_context_as_nested_headings(anmeldung) -> None:
    listing = render_listing(anmeldung, title="Anmeldung Gymnasium")

    assert "# Anmeldung Gymnasium" in listing
    assert "### Gesetzliche Vertreter" in listing
    assert '- [text] "Familienname:" (name=vater_familienname)' in listing


def test_reading_order_is_preserved(anmeldung) -> None:
    ordered = labels(anmeldung)

    assert ordered.index("Vorname:") < ordered.index("Beruf:")


def test_a_real_page_reduces_to_a_listing_the_model_can_read(w3schools) -> None:
    """A 420 KB documentation page must not be what reaches the model."""
    html = (FIXTURES / "w3schools_forms.html").read_text(encoding="utf-8")

    listing = render_listing(w3schools, title="HTML Forms")

    assert len(html) > 400_000
    assert len(listing) < 5_000
    assert "First name:" in listing


def test_no_controls_yields_an_empty_listing() -> None:
    assert build_controls([]) == []
    assert render_listing([]) == ""

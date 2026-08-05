"""Tests for reducing scraped HTML to a listing of form controls.

These run against saved HTML, so they are deterministic and need neither network nor a
running Firecrawl. `anmeldung_form.html` is a purpose-built German questionnaire covering the
structures that matter; `w3schools_forms.html` is a real 420 KB page captured from the live
scraper, kept to prove the reduction holds on genuine markup.

`zetcom_vaadin_form.html` is the grant application at `fpbaselstadtsportamt.zetcom.app`,
captured with the render wait that makes it non-empty. It is the opposite kind of page: a
generated DOM with no `<label>`, no `name`, no heading tag and no `<form>` around any field,
where every association has to be read off the layout. It reduced to twenty unlabelled
controls and the model reported one field, which is what these tests exist to catch.
"""

from pathlib import Path

import pytest

from datenkatalog_attribute_extractor.services.web.html_controls import (
    FormControl,
    extract_controls,
    form_marker_is_informative,
    render_listing,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture(scope="module")
def anmeldung() -> list[FormControl]:
    """Controls of the German questionnaire fixture."""
    return extract_controls((FIXTURES / "anmeldung_form.html").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def vaadin() -> list[FormControl]:
    """Controls of the generated-DOM fixture, which states no associations at all."""
    return extract_controls((FIXTURES / "zetcom_vaadin_form.html").read_text(encoding="utf-8"))


def labels(controls: list[FormControl]) -> list[str]:
    """The labels of the given controls, in order."""
    return [control.label for control in controls]


def find(controls: list[FormControl], name: str) -> FormControl:
    """The control with this `name` attribute."""
    return next(control for control in controls if control.name == name)


def test_hidden_and_submit_controls_are_dropped(anmeldung) -> None:
    names = [control.name for control in anmeldung]

    assert "csrf_token" not in names
    assert not any(control.kind == "submit" for control in anmeldung)


def test_explicit_label_for_is_resolved(anmeldung) -> None:
    assert find(anmeldung, "schueler_familienname").label == "Familienname:"


def test_a_label_wrapping_the_control_is_resolved(anmeldung) -> None:
    """No `for` attribute here; the label encloses the input instead."""
    assert find(anmeldung, "groesse").label == "Grösse (in cm)"


def test_aria_label_is_resolved(anmeldung) -> None:
    assert find(anmeldung, "site_search").label == "Website durchsuchen"


def test_placeholder_is_the_last_resort(anmeldung) -> None:
    assert find(anmeldung, "newsletter").label == "Newsletter abonnieren"


def test_fieldset_legends_become_context(anmeldung) -> None:
    """The Vater/Mutter distinction is exactly what the naming pass needs to disambiguate."""
    vater = find(anmeldung, "vater_familienname")
    mutter = find(anmeldung, "mutter_familienname")

    assert vater.label == mutter.label == "Familienname:"
    assert vater.context_path[-1] == "Vater"
    assert mutter.context_path[-1] == "Mutter"


def test_heading_nesting_becomes_context(anmeldung) -> None:
    assert find(anmeldung, "vater_familienname").context_path == [
        "Anmeldung Gymnasium",
        "Gesetzliche Vertreter",
        "Vater",
    ]


def test_a_radio_group_collapses_to_one_field_named_by_its_legend(anmeldung) -> None:
    """Four radio buttons, one field — the rule the PDF specification already sets out."""
    group = find(anmeldung, "berechtigt")

    assert group.label == "Erziehungs- und Korrespondenzberechtigt"
    assert group.kind == "radio-gruppe"
    assert group.options == ["beide Eltern", "nur Mutter", "nur Vater", "andere"]
    assert len([c for c in anmeldung if c.name == "berechtigt"]) == 1


def test_a_standalone_checkbox_keeps_its_own_caption(anmeldung) -> None:
    """A lone checkbox is not a group, so it is named by its caption, not by its heading."""
    agb = find(anmeldung, "agb")

    assert agb.label == "Ich akzeptiere die Bedingungen"
    assert agb.kind == "checkbox"
    assert agb.options == []


def test_select_options_are_listed(anmeldung) -> None:
    schuljahr = find(anmeldung, "schuljahr")

    assert schuljahr.kind == "auswahlliste"
    assert schuljahr.options == ["2026/27", "2027/28"]


def test_textarea_is_recognised(anmeldung) -> None:
    assert find(anmeldung, "bemerkungen").kind == "textfeld mehrzeilig"


def test_controls_outside_a_form_are_flagged(anmeldung) -> None:
    """Page furniture is the main source of false fields on a scraped page."""
    assert find(anmeldung, "site_search").in_form is False
    assert find(anmeldung, "newsletter").in_form is False
    assert find(anmeldung, "schueler_familienname").in_form is True


def test_document_order_is_preserved(anmeldung) -> None:
    ordered = labels(anmeldung)

    assert ordered.index("Vorname:") < ordered.index("Beruf:")


def test_script_and_style_content_never_reaches_the_listing(anmeldung) -> None:
    listing = render_listing(anmeldung)

    assert "tracking" not in listing
    assert "font-family" not in listing


def test_listing_renders_context_as_nested_headings(anmeldung) -> None:
    listing = render_listing(anmeldung, title="Anmeldung Gymnasium")

    assert "# Anmeldung Gymnasium" in listing
    assert "#### Vater" in listing
    assert '- [text] "Familienname:" (name=vater_familienname)' in listing


def test_empty_html_yields_no_controls() -> None:
    assert extract_controls("") == []
    assert extract_controls("<html><body><p>Kein Formular</p></body></html>") == []


def test_a_real_scraped_page_reduces_by_orders_of_magnitude() -> None:
    """The whole point: 420 KB of real HTML must not be what we send to the model."""
    html = (FIXTURES / "w3schools_forms.html").read_text(encoding="utf-8")

    listing = render_listing(extract_controls(html))

    assert len(html) > 400_000
    assert len(listing) < 5_000
    assert len(html) / len(listing) > 50


def test_a_neighbouring_caption_labels_a_control_the_markup_says_nothing_about(vaadin) -> None:
    """Vaadin puts the caption in the table cell beside the control and nowhere else."""
    found = labels(vaadin)

    assert "Organisation*" in found
    assert "Telefon tagsüber*" in found
    assert "beantragter Beitrag in Schweizer Franken*" in found


def test_every_control_on_a_generated_page_gets_a_label(vaadin) -> None:
    """The failure this fixture records: 20 of 21 controls came back unlabelled."""
    assert vaadin
    assert all(control.label for control in vaadin)


def test_a_caption_that_follows_its_control_is_found(vaadin) -> None:
    """A tick box is captioned after the box, not before it, and its `<label>` here is empty."""
    assert "Ich habe die Wegleitung gelesen und verstanden *" in labels(vaadin)


def test_a_neighbouring_label_never_displaces_one_the_markup_states(anmeldung) -> None:
    """Proximity is a fallback. A page that says what labels what is still believed."""
    assert find(anmeldung, "schueler_familienname").label == "Familienname:"
    assert find(anmeldung, "groesse").label == "Grösse (in cm)"
    assert find(anmeldung, "site_search").label == "Website durchsuchen"


def test_bold_section_titles_become_context(vaadin) -> None:
    """The sections are a `<b>` and a bold-styled cell; neither is a heading tag."""
    sections = {control.context_path[-1] for control in vaadin}

    assert sections == {"Gesuchsteller", "Angaben zum Gesuch", "Dokumente"}


def test_icon_glyphs_are_not_labels(vaadin) -> None:
    """Font Awesome draws from the private use area, which reads as text and is not."""
    for control in vaadin:
        assert not any("\ue000" <= character <= "\uf8ff" for character in control.label)


def test_the_form_marker_is_dropped_where_it_would_condemn_the_whole_form(vaadin) -> None:
    """One `<form>` around the upload widget, every real field outside it."""
    assert form_marker_is_informative(vaadin) is False
    assert "ausserhalb" not in render_listing(vaadin)


def test_the_form_marker_survives_where_it_discriminates(anmeldung) -> None:
    assert form_marker_is_informative(anmeldung) is True
    assert "[ausserhalb eines <form>]" in render_listing(anmeldung)


def test_a_generated_page_reduces_to_a_listing_the_model_can_read(vaadin) -> None:
    html = (FIXTURES / "zetcom_vaadin_form.html").read_text(encoding="utf-8")

    listing = render_listing(vaadin, title="FoundationPlus | Sportamt Basel Stadt")

    assert len(html) / len(listing) > 50
    assert "(ohne Beschriftung)" not in listing


def test_a_real_scraped_page_still_finds_its_form_fields() -> None:
    html = (FIXTURES / "w3schools_forms.html").read_text(encoding="utf-8")

    found = labels(extract_controls(html))

    assert "First name:" in found
    assert "Last name:" in found

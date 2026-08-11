"""Tests for the label reference sent beside a screenshot.

Its whole job is to be subordinate to the picture: the exact spelling of what is on the screen,
and nothing that could be mistaken for a second inventory.
"""

from datenkatalog_attribute_extractor.services.web.browser_client import ObservedControl
from datenkatalog_attribute_extractor.services.web.tile_hints import MAX_HINT_LINES, render_hints


def control(label: str, *, kind: str = "text") -> ObservedControl:
    """One control as the browser reported it."""
    return ObservedControl(kind=kind, label=label, label_source="markup" if label else "")


def test_each_control_is_one_line_with_its_kind() -> None:
    hints = render_hints([control("Vorname"), control("Geburtsdatum", kind="datum")])

    assert "- [text] Vorname" in hints
    assert "- [datum] Geburtsdatum" in hints


def test_the_reference_says_it_is_only_about_spelling() -> None:
    """Without that framing the model reconciles two readings instead of reading the picture."""
    hints = render_hints([control("Vorname")])

    assert "Schreibweise" in hints
    assert "Bild" in hints


def test_umlauts_and_hyphens_survive_verbatim() -> None:
    """The point of the reference: `Grösse` must not come back as `Groesse`."""
    hints = render_hints([control("AHV-Nummer"), control("Grösse")])

    assert "AHV-Nummer" in hints
    assert "Grösse" in hints


def test_unlabelled_controls_are_left_out() -> None:
    """A control with no label spells nothing; the picture shows the box either way."""
    hints = render_hints([control(""), control("Vorname"), control("")])

    assert hints.count("- [") == 1


def test_a_screen_with_nothing_to_spell_produces_nothing() -> None:
    assert render_hints([control(""), control("")]) == ""
    assert render_hints([]) == ""


def test_the_same_label_twice_is_listed_once() -> None:
    """A reference is a vocabulary, not a count — repetition would read as two fields."""
    hints = render_hints([control("Datum"), control("Datum"), control("datum")])

    assert hints.count("- [text] Datum") == 1


def test_the_same_label_in_two_kinds_is_listed_twice() -> None:
    hints = render_hints([control("Anderes"), control("Anderes", kind="checkbox")])

    assert hints.count("- [") == 2


def test_a_screen_of_a_thousand_controls_is_capped() -> None:
    hints = render_hints([control(f"Feld {index}") for index in range(MAX_HINT_LINES + 20)])

    assert hints.count("- [") == MAX_HINT_LINES

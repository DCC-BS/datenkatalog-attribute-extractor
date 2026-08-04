"""Tests for sizing a control listing to the context actually being served."""

from pathlib import Path

from datenkatalog_attribute_extractor.services.web.chunking import (
    FALLBACK_CONTEXT_TOKENS,
    budget_characters,
    split_controls,
)
from datenkatalog_attribute_extractor.services.web.html_controls import FormControl, extract_controls

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

PRODUCTION_CONTEXT = 250_000
DEVELOPMENT_CONTEXT = 16_384


def make_controls(count: int, *, section: str = "Abschnitt") -> list[FormControl]:
    """Build controls spread over distinct sections, so there are boundaries to split on."""
    return [
        FormControl(
            kind="text",
            label=f"Feld {index} mit einer recht ausführlichen Beschriftung",
            context_path=[f"{section} {index // 3}"],
            name=f"feld_{index}",
        )
        for index in range(count)
    ]


def test_no_controls_yields_no_chunks() -> None:
    assert split_controls([], served_context_tokens=PRODUCTION_CONTEXT) == []


def test_a_page_that_fits_is_a_single_chunk() -> None:
    chunks = split_controls(make_controls(20), served_context_tokens=PRODUCTION_CONTEXT)

    assert len(chunks) == 1
    assert chunks[0].index == 1
    assert chunks[0].total == 1
    assert chunks[0].control_count == 20


def test_a_real_form_is_one_call_at_production_context() -> None:
    """The 250k box must not be paying for splits it does not need."""
    controls = extract_controls((FIXTURES / "anmeldung_form.html").read_text(encoding="utf-8"))

    chunks = split_controls(controls, served_context_tokens=PRODUCTION_CONTEXT, title="Anmeldung")

    assert len(chunks) == 1


def test_a_real_form_is_still_one_call_on_the_development_box() -> None:
    """The listing is small enough that 16384 tokens is not a constraint for normal forms."""
    controls = extract_controls((FIXTURES / "anmeldung_form.html").read_text(encoding="utf-8"))

    chunks = split_controls(controls, served_context_tokens=DEVELOPMENT_CONTEXT, title="Anmeldung")

    assert len(chunks) == 1


def test_a_large_page_splits_when_the_context_is_small() -> None:
    chunks = split_controls(make_controls(400), served_context_tokens=4096)

    assert len(chunks) > 1
    assert [chunk.index for chunk in chunks] == list(range(1, len(chunks) + 1))
    assert all(chunk.total == len(chunks) for chunk in chunks)


def test_splitting_never_loses_a_control() -> None:
    """Truncation is the failure this whole design exists to avoid."""
    controls = make_controls(400)

    chunks = split_controls(controls, served_context_tokens=4096)

    assert sum(chunk.control_count for chunk in chunks) == len(controls)


def test_every_chunk_repeats_its_heading_chain() -> None:
    """A chunk that dropped its headings would produce fields the naming pass cannot tell apart."""
    controls = [
        FormControl(kind="text", label="Familienname:", context_path=["Vertreter", "Vater"], name="a"),
        FormControl(kind="text", label="Familienname:", context_path=["Vertreter", "Mutter"], name="b"),
    ]

    chunks = split_controls(controls, served_context_tokens=None)

    for chunk in chunks:
        assert "Vertreter" in chunk.text


def test_the_same_page_needs_fewer_chunks_with_more_context() -> None:
    controls = make_controls(400)

    small = split_controls(controls, served_context_tokens=4096)
    large = split_controls(controls, served_context_tokens=PRODUCTION_CONTEXT)

    assert len(large) < len(small)


def test_an_unreported_context_falls_back_rather_than_assuming_plenty() -> None:
    """Guessing high would fail the call; guessing low only costs an extra chunk."""
    assert budget_characters(None) == budget_characters(FALLBACK_CONTEXT_TOKENS)


def test_the_budget_grows_with_the_served_context() -> None:
    assert budget_characters(PRODUCTION_CONTEXT) > budget_characters(DEVELOPMENT_CONTEXT)


def test_the_budget_never_goes_negative_on_a_tiny_context() -> None:
    assert budget_characters(512) > 0

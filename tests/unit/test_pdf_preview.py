"""Tests for the page previews shown next to the field table."""

from pathlib import Path

import pytest

from datenkatalog_attribute_extractor.ui.pdf_preview import PREVIEW_DPI, render_previews

EXAMPLE_PDF = Path(__file__).resolve().parents[2] / "data" / "example.pdf"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def example_pdf_bytes() -> bytes:
    """The bundled example questionnaire."""
    return EXAMPLE_PDF.read_bytes()


def test_render_previews_returns_one_png_per_page(example_pdf_bytes: bytes) -> None:
    previews = render_previews(example_pdf_bytes, dpi=72)

    assert len(previews) == 7
    assert all(preview.startswith(PNG_MAGIC) for preview in previews)


def test_render_previews_with_an_unreadable_pdf_returns_nothing_instead_of_raising() -> None:
    """A broken preview must not take down the review UI; the fields are still usable."""
    assert render_previews(b"not a pdf") == []


def test_preview_dpi_is_lower_than_the_extraction_default() -> None:
    """Previews are for a human on half a screen, not for the model."""
    assert PREVIEW_DPI < 200

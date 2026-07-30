"""Tests for PDF page rendering."""

from pathlib import Path

import pytest

from datenkatalog_attribute_extractor.services.rendering.pdf_renderer import (
    PdfRenderError,
    count_pdf_pages,
    render_pdf_pages,
    render_pdf_pages_async,
)

EXAMPLE_PDF = Path(__file__).resolve().parents[2] / "data" / "example.pdf"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def example_pdf_bytes() -> bytes:
    """The bundled example questionnaire."""
    return EXAMPLE_PDF.read_bytes()


def test_count_pdf_pages_returns_the_page_count(example_pdf_bytes: bytes) -> None:
    assert count_pdf_pages(example_pdf_bytes) == 7


def test_count_pdf_pages_with_invalid_content_raises_render_error() -> None:
    with pytest.raises(PdfRenderError):
        count_pdf_pages(b"this is not a pdf")


def test_render_pdf_pages_renders_every_page_as_png(example_pdf_bytes: bytes) -> None:
    pages = render_pdf_pages(example_pdf_bytes, dpi=100, max_pages=30)

    assert len(pages) == 7
    assert [page.page_number for page in pages] == list(range(1, 8))
    assert all(page.png_bytes.startswith(PNG_MAGIC) for page in pages)
    assert all(page.width > 0 and page.height > 0 for page in pages)


def test_render_pdf_pages_honours_the_page_cap(example_pdf_bytes: bytes) -> None:
    pages = render_pdf_pages(example_pdf_bytes, dpi=72, max_pages=3)

    assert [page.page_number for page in pages] == [1, 2, 3]


def test_render_pdf_pages_scales_with_dpi(example_pdf_bytes: bytes) -> None:
    low = render_pdf_pages(example_pdf_bytes, dpi=72, max_pages=1)[0]
    high = render_pdf_pages(example_pdf_bytes, dpi=144, max_pages=1)[0]

    assert high.width > low.width
    assert high.height > low.height


def test_render_pdf_pages_with_invalid_content_raises_render_error() -> None:
    with pytest.raises(PdfRenderError):
        render_pdf_pages(b"not a pdf at all", dpi=72, max_pages=1)


async def test_render_pdf_pages_async_matches_the_synchronous_result(example_pdf_bytes: bytes) -> None:
    pages = await render_pdf_pages_async(example_pdf_bytes, dpi=72, max_pages=2)

    assert [page.page_number for page in pages] == [1, 2]
    assert all(page.png_bytes.startswith(PNG_MAGIC) for page in pages)

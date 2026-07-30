"""Rendering of PDF pages to PNG images.

Uses pypdfium2 (BSD-3/Apache) rather than PyMuPDF (AGPL), and it ships musl wheels so the
alpine runtime image needs no system packages.
"""

import io
from dataclasses import dataclass

import pypdfium2 as pdfium
from anyio.to_thread import run_sync

PDF_POINTS_PER_INCH = 72


class PdfRenderError(RuntimeError):
    """Raised when a PDF cannot be opened or rendered."""


@dataclass(frozen=True, slots=True, kw_only=True)
class PageImage:
    """A single rendered PDF page."""

    page_number: int
    png_bytes: bytes
    width: int
    height: int


def count_pdf_pages(pdf_bytes: bytes) -> int:
    """Return the number of pages in a PDF.

    Args:
        pdf_bytes: The raw PDF file content.

    Returns:
        The page count.

    Raises:
        PdfRenderError: If the document cannot be opened.
    """
    try:
        document = pdfium.PdfDocument(pdf_bytes)
    except Exception as error:
        raise PdfRenderError("Could not open the PDF document") from error

    try:
        return len(document)
    finally:
        document.close()


def render_pdf_pages(pdf_bytes: bytes, *, dpi: int, max_pages: int) -> list[PageImage]:
    """Render the pages of a PDF to PNG images.

    Args:
        pdf_bytes: The raw PDF file content.
        dpi: Target resolution. 200 renders A4 at roughly 1654x2339 pixels.
        max_pages: Stop after this many pages.

    Returns:
        One `PageImage` per rendered page, in document order.

    Raises:
        PdfRenderError: If the document cannot be opened or a page fails to render.
    """
    try:
        document = pdfium.PdfDocument(pdf_bytes)
    except Exception as error:
        raise PdfRenderError("Could not open the PDF document") from error

    scale = dpi / PDF_POINTS_PER_INCH
    images: list[PageImage] = []

    try:
        for index in range(min(len(document), max_pages)):
            try:
                image = document[index].render(scale=scale).to_pil()
            except Exception as error:
                raise PdfRenderError(f"Could not render page {index + 1}") from error

            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            images.append(
                PageImage(
                    page_number=index + 1,
                    png_bytes=buffer.getvalue(),
                    width=image.width,
                    height=image.height,
                )
            )
    finally:
        document.close()

    return images


async def render_pdf_pages_async(pdf_bytes: bytes, *, dpi: int, max_pages: int) -> list[PageImage]:
    """Render PDF pages off the event loop.

    Rasterising is CPU-bound, so it runs in a worker thread to keep the API responsive.

    Args:
        pdf_bytes: The raw PDF file content.
        dpi: Target resolution.
        max_pages: Stop after this many pages.

    Returns:
        One `PageImage` per rendered page, in document order.
    """
    return await run_sync(lambda: render_pdf_pages(pdf_bytes, dpi=dpi, max_pages=max_pages))

"""Page previews for side-by-side review.

Rendered locally in the UI process rather than fetched from the API: the browser already
uploaded the bytes, the rasteriser is the same one the extractor uses, and a round trip per
page would move megabytes for no benefit. `st.pdf` is not usable here because it cannot be
told which page to show, and page-by-page comparison is the entire point of the view.
"""

from datenkatalog_attribute_extractor.services.rendering.pdf_renderer import PdfRenderError, render_pdf_pages

# Lower than the extraction DPI: this is for a human looking at half a screen, not for OCR.
PREVIEW_DPI = 150
MAX_PREVIEW_PAGES = 100


def render_previews(pdf_bytes: bytes, *, dpi: int = PREVIEW_DPI) -> list[bytes]:
    """Render every page of a PDF to a PNG for display.

    Args:
        pdf_bytes: The uploaded PDF.
        dpi: Resolution of the preview images.

    Returns:
        One PNG per page, in document order. Empty if the PDF cannot be rendered.
    """
    try:
        pages = render_pdf_pages(pdf_bytes, dpi=dpi, max_pages=MAX_PREVIEW_PAGES)
    except PdfRenderError:
        return []
    return [page.png_bytes for page in pages]

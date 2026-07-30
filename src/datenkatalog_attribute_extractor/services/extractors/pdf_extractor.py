"""Vision-based form field extraction from PDF documents."""

import asyncio
from collections.abc import AsyncIterator

from dcc_backend_common.logger import get_logger

from datenkatalog_attribute_extractor.models.enums import SourceKind
from datenkatalog_attribute_extractor.models.extraction import ExtractionRequest, PageResult
from datenkatalog_attribute_extractor.services.agents.form_field_agent import FormFieldExtractionAgent
from datenkatalog_attribute_extractor.services.llm_health import (
    LlmHealthProbe,
    LlmUnavailableError,
    is_fatal_llm_error,
)
from datenkatalog_attribute_extractor.services.rendering.pdf_renderer import PageImage, render_pdf_pages_async

logger = get_logger(__name__)

PDF_MEDIA_TYPES = frozenset({"application/pdf", "application/x-pdf"})


class PdfFieldExtractor:
    """Extracts form fields from a PDF by rendering each page and reading it with a vision LLM.

    Attributes:
        source_kind: Always `SourceKind.PDF`.
    """

    source_kind = SourceKind.PDF

    def __init__(
        self,
        agent: FormFieldExtractionAgent,
        health_probe: LlmHealthProbe,
        *,
        render_dpi: int,
        max_pages: int,
        max_concurrency: int,
    ) -> None:
        """Initialise the extractor.

        Args:
            agent: The vision agent that reads a single page.
            health_probe: Verifies the LLM is reachable before any work is done.
            render_dpi: Resolution used to rasterise pages.
            max_pages: Hard cap on the number of pages processed per document.
            max_concurrency: How many pages may be in flight at the LLM at once. Keep this
                aligned with the vLLM `--max-num-seqs` setting; a higher value here only
                queues requests inside the server.
        """
        self._agent = agent
        self._health_probe = health_probe
        self._render_dpi = render_dpi
        self._max_pages = max_pages
        self._semaphore = asyncio.Semaphore(max_concurrency)

    def supports(self, media_type: str) -> bool:
        """Report whether the media type is a PDF."""
        return media_type.split(";")[0].strip().lower() in PDF_MEDIA_TYPES

    async def extract(self, request: ExtractionRequest) -> AsyncIterator[PageResult]:
        """Render and read every page of the PDF.

        The LLM is checked first, before anything is rasterised: without it no page can
        succeed, and rendering thirty pages only to fail on each one wastes minutes and
        buries the real cause in warnings.

        A page that fails for its own reasons still yields an empty result plus a warning, so
        one unreadable page cannot lose the others.

        Args:
            request: The uploaded PDF.

        Yields:
            One `PageResult` per page, in document order.

        Raises:
            LlmUnavailableError: If the LLM is unreachable, or becomes unusable mid-run.
        """
        await self._health_probe.ensure_available()

        images = await render_pdf_pages_async(
            request.content,
            dpi=self._render_dpi,
            max_pages=self._max_pages,
        )
        total_pages = len(images)
        logger.info("pdf_rendered", filename=request.filename, pages=total_pages, dpi=self._render_dpi)

        for image in images:
            yield await self._extract_page(image, total_pages)

    async def _extract_page(self, image: PageImage, total_pages: int) -> PageResult:
        """Read one rendered page.

        Page-local failures become warnings; failures that mean the LLM itself is unusable
        abort the run.

        Raises:
            LlmUnavailableError: If the LLM became unreachable or is misconfigured.
        """
        async with self._semaphore:
            try:
                extraction = await self._agent.extract_page(image.png_bytes)
            except Exception as error:
                if is_fatal_llm_error(error):
                    logger.error("llm_call_failed_fatally", page=image.page_number, error=str(error))
                    raise LlmUnavailableError(self._health_probe.url, str(error) or type(error).__name__) from error

                logger.exception("page_extraction_failed", page=image.page_number)
                return PageResult(
                    page=image.page_number,
                    total_pages=total_pages,
                    fields=[],
                    warnings=[f"Page {image.page_number} could not be processed: {error}"],
                )

        fields = extraction.fields
        for field in fields:
            field.page = image.page_number

        warnings = [] if fields else [f"No fields were found on page {image.page_number}"]
        logger.info("page_extracted", page=image.page_number, fields=len(fields))
        return PageResult(
            page=image.page_number,
            total_pages=total_pages,
            fields=fields,
            warnings=warnings,
        )

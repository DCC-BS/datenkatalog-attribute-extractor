"""Form field extraction from a web page."""

import re
from collections.abc import AsyncIterator

from dcc_backend_common.logger import get_logger
from selectolax.parser import HTMLParser

from datenkatalog_attribute_extractor.models.enums import SourceKind
from datenkatalog_attribute_extractor.models.extraction import ExtractionSource, PageResult, UrlSource
from datenkatalog_attribute_extractor.services.agents.web_field_agent import WebFieldExtractionAgent
from datenkatalog_attribute_extractor.services.llm_health import (
    LlmHealthProbe,
    LlmUnavailableError,
    is_fatal_llm_error,
)
from datenkatalog_attribute_extractor.services.web.chunking import ListingChunk, split_controls
from datenkatalog_attribute_extractor.services.web.firecrawl_client import FirecrawlClient
from datenkatalog_attribute_extractor.services.web.html_controls import extract_controls
from datenkatalog_attribute_extractor.services.web.url_policy import ensure_safe_url

logger = get_logger(__name__)

# Wording that marks a page as one step of several. Matching any of these only produces a
# warning: guessing which links are the remaining steps is not something we attempt.
#
# These are phrases, not words. A bare "weiter" matched "Weitere Angaben" — an ordinary
# section heading on an ordinary single-page form — and flagged it as a wizard. Anything short
# enough to appear inside a longer German word is too weak to use here.
MULTI_STEP_PATTERNS = (
    r"\bn(?:ä|ae)chster schritt\b",
    r"\bschritt\s+1\s+von\s+\d+",
    r"\bseite\s+1\s+von\s+\d+",
    r"\bteil\s+1\s+von\s+\d+",
    r"\bstep\s+1\s+of\s+\d+",
    r"\bpage\s+1\s+of\s+\d+",
    r"\bnext step\b",
    r"\bweiter zu\b",
)

_MULTI_STEP = re.compile("|".join(MULTI_STEP_PATTERNS), re.IGNORECASE)


class WebFieldExtractor:
    """Extracts form fields from a single web page.

    One URL, one scrape. A form spread across several pages is not followed: the later steps
    of a wizard usually live behind a submit that requires valid answers, and the links a
    crawler can see are as likely to be navigation as the next step. Where the page looks like
    one step of several, a warning says so rather than the inventory quietly being partial.

    Attributes:
        source_kind: Always `SourceKind.WEB`.
    """

    source_kind = SourceKind.WEB

    def __init__(
        self,
        agent: WebFieldExtractionAgent,
        client: FirecrawlClient,
        health_probe: LlmHealthProbe,
        *,
        max_units: int,
    ) -> None:
        """Initialise the extractor.

        Args:
            agent: The text agent that reads a control listing.
            client: Scrapes the page.
            health_probe: Verifies the LLM before any work, and reports the served context.
            max_units: Hard cap on the chunks processed for one page.
        """
        self._agent = agent
        self._client = client
        self._health_probe = health_probe
        self._max_units = max_units

    def supports(self, source: ExtractionSource) -> bool:
        """Report whether the source is a URL."""
        return isinstance(source, UrlSource)

    async def extract(self, source: ExtractionSource) -> AsyncIterator[PageResult]:
        """Scrape a page and read the form fields off it.

        The order of the checks is deliberate and matches the PDF path: everything that can
        fail for the whole run fails before any model call. The URL policy runs first because
        it is free, then the LLM is verified, then the page is fetched.

        Args:
            source: The URL to read.

        Yields:
            One `PageResult` per listing chunk. A page that fits the context is one result.

        Raises:
            UnsafeUrlError: If the URL is not a public web address.
            LlmUnavailableError: If the LLM is unreachable or misconfigured.
            FirecrawlUnavailableError: If Firecrawl cannot serve the scrape.
            PageUnreadableError: If the page itself could not be read.
            TypeError: If handed a source this extractor does not support.
        """
        if not isinstance(source, UrlSource):
            raise TypeError(f"WebFieldExtractor cannot handle {type(source).__name__}")

        await ensure_safe_url(source.url)
        await self._health_probe.ensure_available()

        page = await self._client.scrape(source.url)
        controls = extract_controls(page.html)
        logger.info("page_controls_extracted", page_url=source.url, controls=len(controls))

        warnings = [*page.warnings, *_multi_step_warnings(page.html, source.url)]

        chunks = split_controls(
            controls,
            served_context_tokens=self._health_probe.served_context_tokens,
            title=page.title,
        )

        if not chunks:
            yield PageResult(
                page=1,
                total_pages=1,
                fields=[],
                warnings=[*warnings, f"No form controls were found at {source.url}"],
            )
            return

        if len(chunks) > self._max_units:
            warnings.append(
                f"The page was split into {len(chunks)} parts but only the first {self._max_units} "
                f"were processed; fields beyond that point are missing"
            )
            chunks = chunks[: self._max_units]

        total = len(chunks)
        for chunk in chunks:
            # Warnings ride on the first result so they are reported once, not per chunk.
            result = await self._extract_chunk(chunk, total)
            if chunk.index == 1:
                result = PageResult(
                    page=result.page,
                    total_pages=result.total_pages,
                    fields=result.fields,
                    warnings=[*warnings, *result.warnings],
                )
            yield result

    async def _extract_chunk(self, chunk: ListingChunk, total: int) -> PageResult:
        """Read one listing chunk.

        Chunk-local failures become warnings; failures meaning the LLM is unusable abort.

        Raises:
            LlmUnavailableError: If the LLM became unreachable or is misconfigured.
        """
        try:
            extraction = await self._agent.extract_listing(chunk.text)
        except Exception as error:
            if is_fatal_llm_error(error):
                logger.error("llm_call_failed_fatally", chunk=chunk.index, error=str(error))
                raise LlmUnavailableError(self._health_probe.url, str(error) or type(error).__name__) from error

            logger.exception("chunk_extraction_failed", chunk=chunk.index)
            return PageResult(
                page=chunk.index,
                total_pages=total,
                fields=[],
                warnings=[f"Part {chunk.index} of the page could not be processed: {error}"],
            )

        fields = extraction.fields
        for field in fields:
            field.page = chunk.index

        warnings = [] if fields else [f"No fields were found in part {chunk.index} of the page"]
        logger.info("chunk_extracted", chunk=chunk.index, fields=len(fields))
        return PageResult(page=chunk.index, total_pages=total, fields=fields, warnings=warnings)


def _multi_step_warnings(html: str, url: str) -> list[str]:
    """Warn when the page looks like one step of a multi-step form.

    Only the first page is scraped, so a wizard yields a partial inventory that looks
    complete. This cannot be detected reliably — the wording is a heuristic — so it warns and
    never fails.

    Only the rendered text is searched, never the markup. Class names, data attributes and
    inline scripts are full of words like "next" and "step" that say nothing about whether a
    person is looking at step one of a wizard.

    Args:
        html: The scraped page.
        url: The page's URL, for the message.

    Returns:
        A single warning, or nothing.
    """
    tree = HTMLParser(html)
    for tag in ("script", "style", "noscript"):
        for node in tree.css(tag):
            node.decompose()

    body = tree.body
    text = " ".join((body.text() if body is not None else "").split())

    if _MULTI_STEP.search(text):
        return [
            f"The page at {url} looks like one step of a multi-step form. "
            "Only this step was read; any further steps are not included"
        ]
    return []

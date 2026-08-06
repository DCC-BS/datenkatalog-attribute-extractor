"""Form field extraction from a web page, read from the DOM or from the picture of it."""

import re
from collections.abc import AsyncIterator

from dcc_backend_common.logger import get_logger

from datenkatalog_attribute_extractor.models.enums import SourceKind
from datenkatalog_attribute_extractor.models.extraction import ExtractionSource, PageResult, UrlSource
from datenkatalog_attribute_extractor.services.agents.form_field_agent import FormFieldExtractionAgent
from datenkatalog_attribute_extractor.services.agents.web_field_agent import WebFieldExtractionAgent
from datenkatalog_attribute_extractor.services.llm_health import (
    LlmHealthProbe,
    LlmUnavailableError,
    is_fatal_llm_error,
)
from datenkatalog_attribute_extractor.services.web.browser_client import BrowserClient, ObservedPage
from datenkatalog_attribute_extractor.services.web.chunking import ListingChunk, split_controls
from datenkatalog_attribute_extractor.services.web.controls import build_controls
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

    The page is rendered once and read twice over. The DOM reading is preferred wherever it
    works: it gives labels verbatim, states which tick boxes form one group, and costs one
    call for a whole page rather than one per screen. But it only works where the page carries
    the information — a form drawn on a canvas, painted as an image, or built so that no text
    can be tied to any control leaves it with an inventory of nameless boxes.

    Rather than guess which page is which, the reading is *measured*: the share of controls
    that came away with a label. Below a threshold the same render's screenshots go to the
    vision agent that already reads PDF pages, and the result says which reading was used.
    A page spread across several URLs is not followed; where it looks like one step of
    several, a warning says so rather than the inventory quietly being partial.

    Attributes:
        source_kind: Always `SourceKind.WEB`.
    """

    source_kind = SourceKind.WEB

    def __init__(
        self,
        agent: WebFieldExtractionAgent,
        vision_agent: FormFieldExtractionAgent,
        client: BrowserClient,
        health_probe: LlmHealthProbe,
        *,
        max_units: int,
        min_labelled_share: float,
    ) -> None:
        """Initialise the extractor.

        Args:
            agent: The text agent that reads a control listing.
            vision_agent: The agent that reads a rendered page image, shared with the PDF path.
            client: Renders the page and reports what it looks like.
            health_probe: Verifies the LLM before any work, and reports the served context.
            max_units: Hard cap on the units of work — listing chunks or screenshot tiles.
            min_labelled_share: The share of controls that must carry a label for the DOM
                reading to be trusted. Below it the screenshots are read instead.
        """
        self._agent = agent
        self._vision_agent = vision_agent
        self._client = client
        self._health_probe = health_probe
        self._max_units = max_units
        self._min_labelled_share = min_labelled_share

    def supports(self, source: ExtractionSource) -> bool:
        """Report whether the source is a URL."""
        return isinstance(source, UrlSource)

    async def extract(self, source: ExtractionSource) -> AsyncIterator[PageResult]:
        """Render a page and read the form fields off it.

        The order of the checks is deliberate and matches the PDF path: everything that can
        fail for the whole run fails before any model call. The URL policy runs first because
        it is free, then the LLM is verified, then the page is rendered.

        Args:
            source: The URL to read.

        Yields:
            One `PageResult` per unit of work: a listing chunk, or a screenshot tile.

        Raises:
            UnsafeUrlError: If the URL is not a public web address.
            LlmUnavailableError: If the LLM is unreachable or misconfigured.
            BrowserUnavailableError: If the browser service cannot serve the render.
            PageUnreadableError: If the page itself could not be loaded.
            TypeError: If handed a source this extractor does not support.
        """
        if not isinstance(source, UrlSource):
            raise TypeError(f"WebFieldExtractor cannot handle {type(source).__name__}")

        await ensure_safe_url(source.url)
        await self._health_probe.ensure_available()

        page = await self._client.observe(source.url)
        warnings = _multi_step_warnings(page.text, source.url)

        if self._dom_reading_is_usable(page, forced_to_screenshots=source.force_screenshots):
            async for result in self._extract_from_listing(page, source.url, warnings):
                yield result
            return

        async for result in self._extract_from_screenshots(
            page, source.url, warnings, requested=source.force_screenshots
        ):
            yield result

    def _dom_reading_is_usable(self, page: ObservedPage, *, forced_to_screenshots: bool) -> bool:
        """Report whether the rendered DOM said enough about this page to be read from.

        Args:
            page: The rendered page.
            forced_to_screenshots: Whether the caller asked for the screenshot reading outright.

        Returns:
            Whether to read the control listing rather than the screenshots.
        """
        usable = not forced_to_screenshots and bool(page.controls) and page.labelled_share >= self._min_labelled_share
        logger.info(
            "web_reading_chosen",
            reading="dom" if usable else "vision",
            forced=forced_to_screenshots,
            controls=len(page.controls),
            labelled_share=round(page.labelled_share, 2),
            threshold=self._min_labelled_share,
        )
        return usable

    async def _extract_from_listing(
        self, page: ObservedPage, page_url: str, warnings: list[str]
    ) -> AsyncIterator[PageResult]:
        """Read the page from its control listing, one chunk of the listing per result."""
        controls = build_controls(page.controls)
        chunks = split_controls(
            controls,
            served_context_tokens=self._health_probe.served_context_tokens,
            title=page.title,
        )

        if len(chunks) > self._max_units:
            warnings.append(
                f"The page was split into {len(chunks)} parts but only the first {self._max_units} "
                f"were processed; fields beyond that point are missing"
            )
            chunks = chunks[: self._max_units]

        total = len(chunks)
        for chunk in chunks:
            result = await self._extract_chunk(chunk, total)
            # Warnings ride on the first result so they are reported once, not per chunk.
            if chunk.index == 1:
                result = PageResult(
                    page=result.page,
                    total_pages=result.total_pages,
                    fields=result.fields,
                    warnings=[*warnings, *result.warnings],
                )
            yield result

    async def _extract_from_screenshots(
        self, page: ObservedPage, page_url: str, warnings: list[str], *, requested: bool = False
    ) -> AsyncIterator[PageResult]:
        """Read the page from its screenshots, one tile per result.

        This is the same agent and the same prompt the PDF path uses. A screenshot of a form is
        a picture of a form page, which is exactly what that agent was built for.
        """
        tiles = page.screenshots[: self._max_units]

        if not tiles:
            yield PageResult(
                page=1,
                total_pages=1,
                fields=[],
                warnings=[*warnings, f"No form controls were found at {page_url}"],
            )
            return

        if requested:
            warnings.append(f"The page at {page_url} was read from screenshots, as requested")
        else:
            reason = (
                f"No form controls could be read from the page markup at {page_url}"
                if not page.controls
                else f"Only {round(page.labelled_share * 100)}% of the controls at {page_url} had a readable label"
            )
            warnings.append(f"{reason}; the page was read from screenshots instead")

        if len(page.screenshots) > self._max_units:
            warnings.append(
                f"The page filled {len(page.screenshots)} screens but only the first {self._max_units} "
                f"were processed; fields beyond that point are missing"
            )

        total = len(tiles)
        seen_on_previous_tile: set[str] = set()

        for index, tile in enumerate(tiles, start=1):
            result = await self._extract_tile(tile, index, total)

            # Tiles overlap so that a field cut in half by one boundary is whole in the next,
            # which means the fields in the overlap are reported twice. A field with the same
            # label under the same headings, seen on two adjoining screens, is that field
            # again — the alternative reading, a form that repeats one field in one section, is
            # not something a form does.
            kept = [field for field in result.fields if _field_key(field) not in seen_on_previous_tile]
            duplicates = len(result.fields) - len(kept)
            seen_on_previous_tile = {_field_key(field) for field in result.fields}

            extra = warnings if index == 1 else []
            if duplicates:
                logger.info("overlapping_fields_merged", tile=index, duplicates=duplicates)
                extra = [
                    *extra,
                    f"{duplicates} field(s) visible on both screen {index - 1} and screen {index} were reported once",
                ]

            # The tile rides along with the fields read off it: it is the only picture of what
            # the model saw, and reopening the live page gives a fresh render, not this one.
            yield PageResult(
                page=result.page,
                total_pages=result.total_pages,
                fields=kept,
                warnings=[*extra, *result.warnings],
                image=tile,
            )

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

    async def _extract_tile(self, tile: bytes, index: int, total: int) -> PageResult:
        """Read one screenshot tile.

        Raises:
            LlmUnavailableError: If the LLM became unreachable or is misconfigured.
        """
        try:
            extraction = await self._vision_agent.extract_page(tile)
        except Exception as error:
            if is_fatal_llm_error(error):
                logger.error("llm_call_failed_fatally", tile=index, error=str(error))
                raise LlmUnavailableError(self._health_probe.url, str(error) or type(error).__name__) from error

            logger.exception("tile_extraction_failed", tile=index)
            return PageResult(
                page=index,
                total_pages=total,
                fields=[],
                warnings=[f"Screen {index} of the page could not be processed: {error}"],
            )

        fields = extraction.fields
        for field in fields:
            field.page = index

        warnings = [] if fields else [f"No fields were found on screen {index} of the page"]
        logger.info("tile_extracted", tile=index, fields=len(fields))
        return PageResult(page=index, total_pages=total, fields=fields, warnings=warnings)


def _field_key(field) -> str:
    """What makes two fields on adjoining screens the same field.

    The label alone, not the label and its headings: a heading that scrolled off the top of
    the second screen is not reported with the fields under it, so the same field comes back
    with a different `context_path` on each screen. Comparing labels alone can in principle
    merge two genuinely different fields that share a label and fall either side of one
    boundary; that is rarer than the duplication it prevents, and it is reported either way.
    """
    return field.label.strip().casefold()


def _multi_step_warnings(text: str, url: str) -> list[str]:
    """Warn when the page looks like one step of a multi-step form.

    Only the first page is read, so a wizard yields a partial inventory that looks complete.
    This cannot be detected reliably — the wording is a heuristic — so it warns and never
    fails. Only the rendered text is searched, never the markup: class names, data attributes
    and inline scripts are full of words like "next" and "step" that say nothing about whether
    a person is looking at step one of a wizard.

    Args:
        text: The page's rendered text.
        url: The page's URL, for the message.

    Returns:
        A single warning, or nothing.
    """
    if _MULTI_STEP.search(" ".join(text.split())):
        return [
            f"The page at {url} looks like one step of a multi-step form. "
            "Only this step was read; any further steps are not included"
        ]
    return []

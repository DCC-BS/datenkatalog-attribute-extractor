"""Form field extraction from a web form, read from pictures of every step of it."""

from collections.abc import AsyncIterator

from dcc_backend_common.logger import get_logger

from datenkatalog_attribute_extractor.models.enums import SourceKind
from datenkatalog_attribute_extractor.models.extraction import ExtractionSource, PageResult, UrlSource
from datenkatalog_attribute_extractor.models.field import ExtractedField
from datenkatalog_attribute_extractor.services.agents.web_field_agent import WebFieldExtractionAgent
from datenkatalog_attribute_extractor.services.llm_health import (
    LlmHealthProbe,
    LlmUnavailableError,
    is_fatal_llm_error,
)
from datenkatalog_attribute_extractor.services.web.browser_client import (
    BrowserClient,
    ObservedPage,
    ObservedStep,
    ObservedTile,
)
from datenkatalog_attribute_extractor.services.web.tile_hints import render_hints
from datenkatalog_attribute_extractor.services.web.url_policy import ensure_safe_url

logger = get_logger(__name__)

# Why the walk ended, in words a reviewer can act on. `no_next` and `max_steps` are reported
# too: a form the walk left early is a partial inventory either way, and the difference between
# "there was nothing more to press" and "we ran out of allowance" is the difference between
# accepting the result and raising the limit.
STOP_REASONS = {
    "unchanged": (
        "the form did not accept the placeholder answers and would not advance past step {steps}; "
        "any later steps are missing"
    ),
    "blocked": ("step {steps} would not let its next button be pressed, so any later steps are missing"),
    "no_next": "no further step button was found after step {steps}",
    "max_steps": "the walk stopped after the allowed {steps} step(s); a longer form would be cut off here",
    "left_site": "following the form led away from the site after step {steps}; the walk stopped there",
}


class WebFieldExtractor:
    """Extracts form fields from an online form, step by step and screen by screen.

    The form is rendered in our own browser, walked through its steps, and photographed. A unit
    of work is a run of consecutive screens of one step — as many as the model takes images in
    one call — together with the labels the browser measured off those same screens, as a
    reference for their spelling.

    Screens go in together rather than one at a time because a form does not break where a
    screen does. A field whose caption ends one screen and whose box begins the next is, to a
    model shown one of them, a caption with nothing under it and a box with no caption, and
    both get reported as fields. Shown the run, it sees one field.

    Reading the picture rather than the markup is a decision made against the alternative. The
    DOM says what a page is built from; it does not say what the page asks. On a generated form
    every heading collapses into one, and the tick boxes of one question read as fifteen
    fields — both of which the rendered page shows plainly.

    A form spread over several steps is followed rather than warned about, because the first
    step of a cantonal wizard is routinely five fields out of eighty. Where the walk cannot get
    past a step, the run says so and the inventory is knowingly partial.

    Attributes:
        source_kind: Always `SourceKind.WEB`.
    """

    source_kind = SourceKind.WEB

    def __init__(
        self,
        agent: WebFieldExtractionAgent,
        client: BrowserClient,
        health_probe: LlmHealthProbe,
        *,
        max_units: int,
        max_steps: int,
        screens_per_call: int,
    ) -> None:
        """Initialise the extractor.

        Args:
            agent: The vision agent that reads the screens of a form.
            client: Renders the form, walks its steps and photographs them.
            health_probe: Verifies the LLM before any work.
            max_units: Hard cap on the screens read, across all steps.
            max_steps: Hard cap on the steps walked.
            screens_per_call: How many screens go into one model call. Bounded by what the
                served model accepts — vLLM refuses a request with more images than
                `--limit-mm-per-prompt` allows.
        """
        self._agent = agent
        self._client = client
        self._health_probe = health_probe
        self._max_units = max_units
        self._max_steps = max_steps
        self._screens_per_call = max(1, screens_per_call)

    def supports(self, source: ExtractionSource) -> bool:
        """Report whether the source is a URL."""
        return isinstance(source, UrlSource)

    async def extract(self, source: ExtractionSource) -> AsyncIterator[PageResult]:
        """Render a form, walk it, and read the fields off every screen.

        The order of the checks is deliberate and matches the PDF path: everything that can
        fail for the whole run fails before any model call. The URL policy runs first because
        it is free, then the LLM is verified, then the form is rendered.

        Args:
            source: The URL to read.

        Yields:
            One `PageResult` per model call: a run of consecutive screens of one step.

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

        page = await self._client.observe(
            source.url,
            max_steps=self._max_steps if source.follow_steps else 1,
        )

        units = self._plan(page)
        warnings = self._opening_warnings(page, units, followed=source.follow_steps)

        if not units:
            yield PageResult(
                page=1,
                total_pages=1,
                fields=[],
                warnings=[*warnings, f"Nothing could be photographed at {source.url}"],
            )
            return

        async for result in self._read_units(units, warnings):
            yield result

    def _plan(self, page: ObservedPage) -> list[tuple[ObservedStep, list[ObservedTile]]]:
        """Group the walk into the calls it will take, capped at what the run is allowed.

        Screens are grouped within a step and never across one. Two steps are two different
        pictures of the form, and a field cut off at the end of one does not continue at the
        start of the next.
        """
        screens = [(step, tile) for step in page.steps for tile in step.tiles][: self._max_units]

        calls: list[tuple[ObservedStep, list[ObservedTile]]] = []
        for step, tile in screens:
            last = calls[-1] if calls else None
            if last and last[0] is step and len(last[1]) < self._screens_per_call:
                last[1].append(tile)
                continue
            calls.append((step, [tile]))
        return calls

    def _opening_warnings(
        self, page: ObservedPage, units: list[tuple[ObservedStep, list[ObservedTile]]], *, followed: bool
    ) -> list[str]:
        """What the reviewer has to know about the walk before reading the fields."""
        warnings: list[str] = []
        steps = len(page.steps)

        if not followed:
            warnings.append("Only the first step of the form was read, as requested")
        elif steps > 1:
            warnings.append(f"The form was followed through {steps} steps")

        # Reported whenever the walk ended for any reason other than having read everything it
        # was asked to, which on a single-step page is the ordinary case and worth no warning.
        if followed and steps > 1 or page.stopped_because in {"unchanged", "blocked"}:
            reason = STOP_REASONS.get(page.stopped_because)
            if reason:
                warnings.append(f"Note: {reason.format(steps=steps)}")

        # Which fields held it up, where the browser could tell. Without this a stuck walk is a
        # partial inventory with no way to judge how partial.
        refused = page.steps[-1].blocked_by if page.steps else []
        if refused:
            warnings.append(f"The form would not accept: {', '.join(refused)}")

        photographed = sum(len(step.tiles) for step in page.steps)
        read = sum(len(tiles) for _, tiles in units)
        if photographed > read:
            warnings.append(
                f"The form filled {photographed} screens but only the first {read} were processed; "
                f"fields beyond that point are missing"
            )

        logger.info(
            "web_walk_planned",
            page_url=page.url,
            steps=steps,
            screens=read,
            calls=len(units),
            stopped_because=page.stopped_because,
        )
        return warnings

    async def _read_units(
        self, units: list[tuple[ObservedStep, list[ObservedTile]]], warnings: list[str]
    ) -> AsyncIterator[PageResult]:
        """Read every run of screens in turn, dropping fields an earlier call already reported."""
        total = len(units)
        reported: set[tuple[str, tuple[str, ...]]] = set()
        previous_call: set[str] = set()
        previous_step = 0

        for index, (step, tiles) in enumerate(units, start=1):
            result = await self._extract_screens(tiles, index, total, step)

            if step.index != previous_step:
                previous_call = set()
                previous_step = step.index

            kept = [field for field in result.fields if not self._is_repeat(field, reported, previous_call)]
            duplicates = len(result.fields) - len(kept)

            previous_call = {_label_key(field) for field in result.fields}
            reported.update(_field_key(field) for field in result.fields)

            extra = list(warnings) if index == 1 else []
            if duplicates:
                logger.info("repeated_fields_dropped", unit=index, duplicates=duplicates)
                extra.append(f"{duplicates} field(s) in part {index} had already been reported and were dropped")

            # The screens ride along with the fields read off them: they are the only picture of
            # what the model saw, and reopening the live page gives a fresh render, not this one.
            yield PageResult(
                page=result.page,
                total_pages=result.total_pages,
                fields=kept,
                warnings=[*extra, *result.warnings],
                images=[tile.image for tile in tiles],
            )

    @staticmethod
    def _is_repeat(
        field: ExtractedField,
        reported: set[tuple[str, tuple[str, ...]]],
        previous_call: set[str],
    ) -> bool:
        """Whether this field has already been reported, by either of two different rules.

        One call ends on the screen the next one begins beside, and adjoining screens overlap
        by design, so a field on that boundary is read twice; there the label alone decides,
        because a heading that scrolled off the top is not reported with the fields under it
        and the `context_path` differs. Within a call the model is told to report such a field
        once, and does — this catches the seam between calls.

        Across steps the label alone is too blunt — a wizard asks for `Vorname` under
        `Meldende Person` and again under `Betroffene Person`, and those are two fields. There
        the headings have to match as well. That rule matters because a form service commonly
        *adds* each step to the page rather than replacing it, so step three is photographed
        with all of step two still on it.
        """
        return _label_key(field) in previous_call or _field_key(field) in reported

    async def _extract_screens(
        self, tiles: list[ObservedTile], index: int, total: int, step: ObservedStep
    ) -> PageResult:
        """Read one run of consecutive screens in a single call.

        Call-local failures become warnings; failures meaning the LLM is unusable abort.

        Raises:
            LlmUnavailableError: If the LLM became unreachable or is misconfigured.
        """
        controls = [control for tile in tiles for control in tile.controls]

        try:
            extraction = await self._agent.extract_screens([tile.image for tile in tiles], render_hints(controls))
        except Exception as error:
            if is_fatal_llm_error(error):
                logger.error("llm_call_failed_fatally", unit=index, error=str(error))
                raise LlmUnavailableError(self._health_probe.url, str(error) or type(error).__name__) from error

            logger.exception("unit_extraction_failed", unit=index)
            return PageResult(
                page=index,
                total_pages=total,
                fields=[],
                warnings=[f"{_describe(index, step, len(tiles))} could not be processed: {error}"],
            )

        fields = extraction.fields
        for field in fields:
            field.page = index

        warnings = [] if fields else [f"No fields were found in {_describe(index, step, len(tiles)).lower()}"]
        logger.info("unit_extracted", unit=index, step=step.index, screens=len(tiles), fields=len(fields))
        return PageResult(page=index, total_pages=total, fields=fields, warnings=warnings)


def _describe(index: int, step: ObservedStep, screens: int) -> str:
    """How one unit of work is referred to in a warning."""
    where = f"step {step.index}" if not step.label else f"step {step.index} ({step.label})"
    screen_word = "screen" if screens == 1 else f"{screens} screens"
    return f"Part {index} of {where} ({screen_word})"


def _label_key(field: ExtractedField) -> str:
    """What makes two fields on adjoining screens the same field."""
    return field.label.strip().casefold()


def _field_key(field: ExtractedField) -> tuple[str, tuple[str, ...]]:
    """What makes two fields in different steps the same field."""
    return _label_key(field), tuple(heading.strip().casefold() for heading in field.context_path)

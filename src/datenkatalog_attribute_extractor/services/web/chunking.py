"""Splits a control listing to fit the context the model is actually being served.

Production serves 250000 tokens and the development box serves 16384 — the same form is one
call in one place and several in the other. Rather than pick a number and be wrong in both,
the budget comes from the `max_model_len` the provider reports, which the LLM health probe
already reads on its way to validating the model.

Splitting happens on section boundaries, never mid-section, and every chunk repeats the
heading chain it sits under. A field's `context_path` is what disambiguates `Familienname`
under *Vater* from the one under *Mutter*, so a chunk that dropped its headings would produce
fields the naming pass cannot tell apart — a split would silently change the output, which is
the one thing it must not do.

Truncation is deliberately not an option. Quietly returning half a form is the failure the
PDF path was rebuilt to eliminate, and it would be worse here: nothing about a short answer
would look wrong.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from dcc_backend_common.logger import get_logger

from datenkatalog_attribute_extractor.services.web.html_controls import FormControl, render_listing

logger = get_logger(__name__)

# Rough characters-per-token for German text with markup. Deliberately pessimistic: counting
# properly needs the model's tokeniser, and over-estimating costs an extra chunk while
# under-estimating costs a failed call halfway through a run.
CHARS_PER_TOKEN = 3

# Reserved out of the context for the instructions, the schema and the model's own answer.
# The answer is the large part: one JSON object per field, for every field on the page.
RESERVED_TOKENS = 3000

# Used when the provider will not say how much context it serves.
FALLBACK_CONTEXT_TOKENS = 8192

# Floor for the per-call budget, so an absurdly small context still yields chunks rather than
# a non-positive size. Kept well below RESERVED_TOKENS: flooring at the reserve would hand a
# 4096-token context a 3000-token budget it does not have. Contexts that small are refused by
# the health probe long before they reach here.
MIN_BUDGET_TOKENS = 500


@dataclass(frozen=True, slots=True, kw_only=True)
class ListingChunk:
    """One unit of work: a listing small enough to send, and where it sits in the document."""

    index: int
    total: int
    text: str
    control_count: int


def budget_characters(served_context_tokens: int | None) -> int:
    """Work out how many characters of listing one call may carry.

    Args:
        served_context_tokens: The provider's reported `max_model_len`, if it reported one.

    Returns:
        The maximum listing size in characters.
    """
    context = served_context_tokens or FALLBACK_CONTEXT_TOKENS
    usable = max(context - RESERVED_TOKENS, MIN_BUDGET_TOKENS)
    return usable * CHARS_PER_TOKEN


def _section_key(control: FormControl) -> tuple[str, ...]:
    """The section a control belongs to, used as a boundary we will not split inside."""
    return tuple(control.context_path)


def split_controls(
    controls: Sequence[FormControl],
    *,
    served_context_tokens: int | None,
    title: str = "",
) -> list[ListingChunk]:
    """Group controls into listings that each fit the served context.

    Every control is placed in some chunk. Capping the amount of work is the caller's job,
    because only the caller can warn the reviewer that it did so.

    Args:
        controls: Every control found on the page, in document order.
        served_context_tokens: The provider's reported `max_model_len`, if known.
        title: The page title, repeated on every chunk for orientation.

    Returns:
        The chunks to send, in document order. A page that fits is a single chunk.
    """
    if not controls:
        return []

    limit = budget_characters(served_context_tokens)
    whole = render_listing(list(controls), title=title)

    if len(whole) <= limit:
        return [ListingChunk(index=1, total=1, text=whole, control_count=len(controls))]

    logger.info(
        "listing_too_large_for_context",
        listing_chars=len(whole),
        limit_chars=limit,
        served_context_tokens=served_context_tokens,
    )

    groups: list[list[FormControl]] = []
    current: list[FormControl] = []
    current_key: tuple[str, ...] | None = None

    for control in controls:
        key = _section_key(control)
        candidate = [*current, control]

        # A section is kept whole where possible: only start a new chunk at a boundary.
        if current and key != current_key and len(render_listing(candidate, title=title)) > limit:
            groups.append(current)
            current = [control]
        else:
            current = candidate
        current_key = key

    if current:
        groups.append(current)

    # A single section can still exceed the budget on its own; that is accepted rather than
    # split mid-section, because the model tolerates an over-long prompt far better than it
    # tolerates fields whose context has been cut away from them.
    total = len(groups)
    return [
        ListingChunk(
            index=position,
            total=total,
            text=render_listing(group, title=title),
            control_count=len(group),
        )
        for position, group in enumerate(groups, start=1)
    ]

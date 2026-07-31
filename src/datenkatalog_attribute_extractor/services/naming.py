"""Deterministic assignment of document-wide unique names to extracted fields.

The extraction agent supplies labels and their enclosing structure; this module turns
that into unique snake_case names. Keeping the logic here rather than in the prompt makes
uniqueness a guarantee instead of a hope, and makes it unit-testable.
"""

import re
import unicodedata
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from datenkatalog_attribute_extractor.models.field import ExtractedField, FormField

MAX_NAME_LENGTH = 64
FALLBACK_NAME = "feld"
FALLBACK_DISPLAY_NAME = "Feld"

# Leading decoration used on printed forms to mark footnotes or list items.
LABEL_DECORATION = "*-–—•·. \t"

# Marks the warnings that merely record a rename, so the UI can keep them apart from
# warnings that describe an actual problem.
DUPLICATE_WARNING_PREFIX = "Duplicate label"

_TRANSLITERATIONS = str.maketrans(
    {
        "ä": "ae",
        "ö": "oe",
        "ü": "ue",
        "ß": "ss",
        "å": "a",
        "æ": "ae",
        "ø": "oe",
        "œ": "oe",
        "đ": "d",
        "ł": "l",
        "þ": "th",
    }
)

_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class FieldNaming:
    """The two names assigned to one field.

    Both are derived from the same label and the same disambiguation decision, so they can
    never describe different things.
    """

    name: str
    display_name: str


def slugify(text: str) -> str:
    """Convert a human label into a snake_case ASCII slug.

    German umlauts and eszett are transliterated rather than stripped, so that
    "Straße" becomes "strasse" and not "strae".

    Args:
        text: The raw label, possibly containing umlauts, punctuation and a trailing colon.

    Returns:
        A lowercase slug of `[a-z0-9_]`, or an empty string if nothing survives.

    Example:
        >>> slugify("Geburtsdatum/Ort:")
        'geburtsdatum_ort'
        >>> slugify("*Strasse:")
        'strasse'
        >>> slugify("Grösse (in cm)")
        'groesse_in_cm'
    """
    lowered = text.strip().rstrip(":").lower()
    transliterated = lowered.translate(_TRANSLITERATIONS)
    decomposed = unicodedata.normalize("NFKD", transliterated)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    return _NON_ALPHANUMERIC.sub("_", ascii_only).strip("_")


def humanise(text: str) -> str:
    """Clean a printed label without normalising it.

    Only form decoration is removed — a trailing colon and leading markers such as `*`.
    Casing, spaces, hyphens and umlauts are left exactly as printed, which is the whole
    point: `AHV-Nummer` must stay `AHV-Nummer` and not become `Ahv Nummer`.

    Args:
        text: The label or heading as it appears on the page.

    Returns:
        The cleaned text, possibly empty.

    Example:
        >>> humanise("*Strasse:")
        'Strasse'
        >>> humanise("AHV-Nummer:")
        'AHV-Nummer'
        >>> humanise("Grösse (in cm)")
        'Grösse (in cm)'
    """
    return text.strip().rstrip(":").strip().lstrip(LABEL_DECORATION).strip()


def _compose_display(segments: Sequence[str], label: str) -> str:
    """Join the disambiguating headings and the label into a readable name."""
    parts = [part for part in (humanise(segment) for segment in segments) if part]
    parts.append(humanise(label) or FALLBACK_DISPLAY_NAME)
    return " ".join(parts)


def _compose(prefix: str, base: str) -> str:
    """Join a disambiguating prefix onto a base name, honouring the length cap.

    The prefix is what makes the name unique, so the base is truncated first.
    """
    if not prefix:
        return base[:MAX_NAME_LENGTH].strip("_")

    room = MAX_NAME_LENGTH - len(prefix) - 1
    if room < 1:
        return f"{prefix}_{base}"[:MAX_NAME_LENGTH].strip("_")
    return f"{prefix}_{base[:room]}".strip("_")


def _segments_at_depth(field: ExtractedField, depth: int) -> list[str]:
    """Return the innermost `depth` headings of the field's context path."""
    return list(field.context_path[-depth:]) if depth else []


def _prefix_at_depth(field: ExtractedField, depth: int) -> str:
    """Build a prefix from the innermost `depth` segments of the field's context path."""
    slugs = [slug for slug in (slugify(segment) for segment in _segments_at_depth(field, depth)) if slug]
    return "_".join(slugs)


def _first_free_numeric(base: str, taken: set[str]) -> tuple[str, int]:
    """Return `base`, or `base_2`, `base_3`, ... — whichever is not yet taken.

    Returns:
        The free name and the numeric suffix used, where 1 means no suffix was needed.
    """
    if base not in taken:
        return base, 1
    suffix = 2
    while True:
        candidate = _compose_suffix(base, str(suffix))
        if candidate not in taken:
            return candidate, suffix
        suffix += 1


def _compose_suffix(base: str, suffix: str) -> str:
    """Append a disambiguating suffix to a base name, honouring the length cap."""
    room = MAX_NAME_LENGTH - len(suffix) - 1
    return f"{base[:room]}_{suffix}" if room >= 1 else f"{base}_{suffix}"[:MAX_NAME_LENGTH]


def _resolve_collision_group(
    fields: Sequence[ExtractedField],
    indices: Sequence[int],
    base: str,
    taken: set[str],
) -> dict[int, FieldNaming]:
    """Assign distinct names to a set of fields that share the same base slug.

    Tries progressively more context: first the innermost enclosing heading, then the two
    innermost, and so on. Falls back to the page number, then to a numeric suffix. Whichever
    step succeeds also shapes the readable name, so the two never disagree about which
    heading did the disambiguating.

    Args:
        fields: All extracted fields, indexed by position.
        indices: Positions in `fields` that collide on `base`.
        base: The shared base slug.
        taken: Names already assigned elsewhere in the document.

    Returns:
        A mapping of field index to its technical and readable name.
    """
    max_depth = max(len(fields[index].context_path) for index in indices)

    for depth in range(1, max_depth + 1):
        candidates = {index: _compose(_prefix_at_depth(fields[index], depth), base) for index in indices}
        values = list(candidates.values())
        if len(set(values)) == len(values) and not set(values) & taken:
            return {
                index: FieldNaming(
                    name=name,
                    display_name=_compose_display(_segments_at_depth(fields[index], depth), fields[index].label),
                )
                for index, name in candidates.items()
            }

    page_candidates = {index: _compose_suffix(base, f"p{fields[index].page}") for index in indices}
    values = list(page_candidates.values())
    if len(set(values)) == len(values) and not set(values) & taken:
        return {
            index: FieldNaming(
                name=name,
                display_name=f"{_compose_display([], fields[index].label)} (Seite {fields[index].page})",
            )
            for index, name in page_candidates.items()
        }

    resolved: dict[int, FieldNaming] = {}
    for index in indices:
        name, suffix = _first_free_numeric(base, taken | {naming.name for naming in resolved.values()})
        readable = _compose_display([], fields[index].label)
        resolved[index] = FieldNaming(
            name=name,
            display_name=readable if suffix == 1 else f"{readable} {suffix}",
        )
    return resolved


def ensure_unique_names(fields: Sequence[ExtractedField]) -> tuple[list[FormField], list[str]]:
    """Give every extracted field a document-wide unique name.

    Fields whose label is already unique keep the plain slug of that label. Every member of
    a colliding group is disambiguated by its enclosing headings, so a form asking twice for
    "Familienname" yields `vater_familienname` and `mutter_familienname` — never a bare
    `familienname` alongside a prefixed sibling.

    Each field also gets a readable `display_name` built from the same label and the same
    disambiguating headings, but without slugifying: `Vater Familienname` beside
    `vater_familienname`.

    Args:
        fields: Extracted fields in document order.

    Returns:
        A tuple of the named fields (in the input order) and a list of human-readable
        warnings describing every rename that was necessary.
    """
    if not fields:
        return [], []

    bases = [slugify(field.label) or FALLBACK_NAME for field in fields]

    by_base: dict[str, list[int]] = defaultdict(list)
    for index, base in enumerate(bases):
        by_base[base].append(index)

    namings: dict[int, FieldNaming] = {}
    taken: set[str] = set()
    warnings: list[str] = []

    for base, indices in by_base.items():
        if len(indices) == 1:
            index = indices[0]
            namings[index] = FieldNaming(name=base, display_name=_compose_display([], fields[index].label))
            taken.add(base)

    for base in sorted(candidate for candidate, indices in by_base.items() if len(indices) > 1):
        resolved = _resolve_collision_group(fields, by_base[base], base, taken)
        for index, naming in sorted(resolved.items()):
            namings[index] = naming
            taken.add(naming.name)
            field = fields[index]
            warnings.append(
                f"{DUPLICATE_WARNING_PREFIX} '{field.label}' on page {field.page} disambiguated to '{naming.name}'"
            )

    named = [
        FormField(
            name=namings[index].name,
            display_name=namings[index].display_name,
            label=field.label,
            context_path=field.context_path,
            page=field.page,
        )
        for index, field in enumerate(fields)
    ]
    return named, warnings

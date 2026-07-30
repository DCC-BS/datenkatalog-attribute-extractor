"""Deterministic assignment of document-wide unique names to extracted fields.

The extraction agent supplies labels and their enclosing structure; this module turns
that into unique snake_case names. Keeping the logic here rather than in the prompt makes
uniqueness a guarantee instead of a hope, and makes it unit-testable.
"""

import re
import unicodedata
from collections import defaultdict
from collections.abc import Sequence

from datenkatalog_attribute_extractor.models.field import ExtractedField, FormField

MAX_NAME_LENGTH = 64
FALLBACK_NAME = "feld"

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


def _prefix_at_depth(field: ExtractedField, depth: int) -> str:
    """Build a prefix from the innermost `depth` segments of the field's context path."""
    segments = field.context_path[-depth:] if depth else []
    slugs = [slug for slug in (slugify(segment) for segment in segments) if slug]
    return "_".join(slugs)


def _first_free_numeric(base: str, taken: set[str]) -> str:
    """Return `base`, or `base_2`, `base_3`, ... — whichever is not yet taken."""
    if base not in taken:
        return base
    suffix = 2
    while True:
        candidate = _compose_suffix(base, str(suffix))
        if candidate not in taken:
            return candidate
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
) -> dict[int, str]:
    """Assign distinct names to a set of fields that share the same base slug.

    Tries progressively more context: first the innermost enclosing heading, then the two
    innermost, and so on. Falls back to the page number, then to a numeric suffix.

    Args:
        fields: All extracted fields, indexed by position.
        indices: Positions in `fields` that collide on `base`.
        base: The shared base slug.
        taken: Names already assigned elsewhere in the document.

    Returns:
        A mapping of field index to unique name.
    """
    max_depth = max(len(fields[index].context_path) for index in indices)

    for depth in range(1, max_depth + 1):
        candidates = {index: _compose(_prefix_at_depth(fields[index], depth), base) for index in indices}
        values = list(candidates.values())
        if len(set(values)) == len(values) and not set(values) & taken:
            return candidates

    page_candidates = {index: _compose_suffix(base, f"p{fields[index].page}") for index in indices}
    values = list(page_candidates.values())
    if len(set(values)) == len(values) and not set(values) & taken:
        return page_candidates

    resolved: dict[int, str] = {}
    for index in indices:
        name = _first_free_numeric(base, taken | set(resolved.values()))
        resolved[index] = name
    return resolved


def ensure_unique_names(fields: Sequence[ExtractedField]) -> tuple[list[FormField], list[str]]:
    """Give every extracted field a document-wide unique name.

    Fields whose label is already unique keep the plain slug of that label. Every member of
    a colliding group is disambiguated by its enclosing headings, so a form asking twice for
    "Familienname" yields `vater_familienname` and `mutter_familienname` — never a bare
    `familienname` alongside a prefixed sibling.

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

    names: dict[int, str] = {}
    taken: set[str] = set()
    warnings: list[str] = []

    for base, indices in by_base.items():
        if len(indices) == 1:
            names[indices[0]] = base
            taken.add(base)

    for base in sorted(candidate for candidate, indices in by_base.items() if len(indices) > 1):
        resolved = _resolve_collision_group(fields, by_base[base], base, taken)
        for index, name in sorted(resolved.items()):
            names[index] = name
            taken.add(name)
            field = fields[index]
            warnings.append(f"Duplicate label '{field.label}' on page {field.page} disambiguated to '{name}'")

    named = [
        FormField(name=names[index], label=field.label, context_path=field.context_path, page=field.page)
        for index, field in enumerate(fields)
    ]
    return named, warnings

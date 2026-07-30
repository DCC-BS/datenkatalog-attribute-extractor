"""Test data builders."""

from datenkatalog_attribute_extractor.models.field import ExtractedField


def make_field(*, label: str, context_path: list[str] | None = None, page: int = 1) -> ExtractedField:
    """Build an `ExtractedField` with sensible defaults.

    Args:
        label: The field label.
        context_path: Enclosing headings, outermost first.
        page: 1-based page number.

    Returns:
        The constructed field.
    """
    return ExtractedField(label=label, context_path=context_path or [], page=page)

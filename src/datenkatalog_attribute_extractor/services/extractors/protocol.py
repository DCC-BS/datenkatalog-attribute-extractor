"""The extension point for new document kinds.

An extractor turns a source into raw, unnamed fields. Everything downstream — naming,
uniqueness, the API contract, the UI — is source-agnostic, so adding Excel support means
adding one extractor and registering it, and nothing else.

Dispatch is on the source object rather than a media type string. A URL has no bytes and no
media type, and forcing one on it would have meant a synthetic type that lies about what the
source is; letting `supports()` see the source keeps every kind honest.
"""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from datenkatalog_attribute_extractor.models.enums import SourceKind
from datenkatalog_attribute_extractor.models.extraction import ExtractionSource, PageResult, UploadSource


class UnsupportedSourceError(ValueError):
    """Raised when no extractor is registered for a source."""

    def __init__(self, description: str) -> None:
        """Initialise the error.

        Args:
            description: What could not be handled, phrased for an API caller.
        """
        super().__init__(f"No extractor registered for {description}")
        self.description = description


@runtime_checkable
class FieldExtractor(Protocol):
    """Extracts unnamed form fields from one kind of source."""

    source_kind: SourceKind

    def supports(self, source: ExtractionSource) -> bool:
        """Report whether this extractor can handle the given source."""
        ...

    def extract(self, source: ExtractionSource) -> AsyncIterator[PageResult]:
        """Extract fields, yielding one result per unit of work as it completes.

        Yielding incrementally is what lets the API report progress on sources that take
        minutes to process.

        Args:
            source: The document or URL to read.

        Yields:
            One `PageResult` per page, chunk or tile, in document order.
        """
        ...


class ExtractorRegistry:
    """Resolves the right extractor for a source."""

    def __init__(self, extractors: list[FieldExtractor]) -> None:
        """Initialise the registry.

        Args:
            extractors: The available extractors, in resolution order.
        """
        self._extractors = extractors

    def resolve(self, source: ExtractionSource) -> FieldExtractor:
        """Find the extractor responsible for a source.

        Args:
            source: The document or URL to read.

        Returns:
            The matching extractor.

        Raises:
            UnsupportedSourceError: If no extractor claims the source.
        """
        for extractor in self._extractors:
            if extractor.supports(source):
                return extractor
        raise UnsupportedSourceError(describe_source(source))


def describe_source(source: ExtractionSource) -> str:
    """Describe a source for an error message an API caller will read.

    Args:
        source: The document or URL that could not be handled.

    Returns:
        A phrase naming what was wrong with it.
    """
    if isinstance(source, UploadSource):
        return f"media type '{source.media_type}'"
    return f"URL '{source.url}'"

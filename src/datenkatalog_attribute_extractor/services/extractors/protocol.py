"""The extension point for new document kinds.

An extractor turns an uploaded document into raw, unnamed fields. Everything downstream —
naming, uniqueness, the API contract, the UI — is source-agnostic, so adding Excel or web
page support means adding one extractor and registering it, and nothing else.
"""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from datenkatalog_attribute_extractor.models.enums import SourceKind
from datenkatalog_attribute_extractor.models.extraction import ExtractionRequest, PageResult


class UnsupportedSourceError(ValueError):
    """Raised when no extractor is registered for an uploaded document."""

    def __init__(self, media_type: str) -> None:
        """Initialise the error.

        Args:
            media_type: The media type that could not be handled.
        """
        super().__init__(f"No extractor registered for media type '{media_type}'")
        self.media_type = media_type


@runtime_checkable
class FieldExtractor(Protocol):
    """Extracts unnamed form fields from one kind of document."""

    source_kind: SourceKind

    def supports(self, media_type: str) -> bool:
        """Report whether this extractor can handle the given media type."""
        ...

    def extract(self, request: ExtractionRequest) -> AsyncIterator[PageResult]:
        """Extract fields, yielding one result per page as it completes.

        Yielding incrementally is what lets the API report progress on documents that take
        minutes to process.

        Args:
            request: The uploaded document.

        Yields:
            One `PageResult` per page, in document order.
        """
        ...


class ExtractorRegistry:
    """Resolves the right extractor for an uploaded document."""

    def __init__(self, extractors: list[FieldExtractor]) -> None:
        """Initialise the registry.

        Args:
            extractors: The available extractors, in resolution order.
        """
        self._extractors = extractors

    def resolve(self, media_type: str) -> FieldExtractor:
        """Find the extractor responsible for a media type.

        Args:
            media_type: The uploaded document's media type.

        Returns:
            The matching extractor.

        Raises:
            UnsupportedSourceError: If no extractor claims the media type.
        """
        for extractor in self._extractors:
            if extractor.supports(media_type):
                return extractor
        raise UnsupportedSourceError(media_type)

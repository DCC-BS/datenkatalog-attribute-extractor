"""Source-agnostic orchestration of a field extraction run."""

from collections.abc import AsyncIterator

from dcc_backend_common.logger import get_logger

from datenkatalog_attribute_extractor.models.extraction import (
    ExtractionProgress,
    ExtractionRequest,
    ExtractionResponse,
)
from datenkatalog_attribute_extractor.models.field import ExtractedField
from datenkatalog_attribute_extractor.services.extractors.protocol import ExtractorRegistry
from datenkatalog_attribute_extractor.services.naming import ensure_unique_names

logger = get_logger(__name__)

type ExtractionEvent = ExtractionProgress | ExtractionResponse


class DocumentTooLargeError(ValueError):
    """Raised when an upload exceeds the configured size limit."""

    def __init__(self, size_bytes: int, limit_bytes: int) -> None:
        """Initialise the error.

        Args:
            size_bytes: Actual upload size.
            limit_bytes: Configured maximum.
        """
        super().__init__(f"Document is {size_bytes} bytes, which exceeds the limit of {limit_bytes} bytes")
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes


class EmptyDocumentError(ValueError):
    """Raised when an upload contains no bytes."""

    def __init__(self) -> None:
        """Initialise the error."""
        super().__init__("The uploaded document is empty")


class ExtractionService:
    """Runs an extraction end to end: dispatch, per-page extraction, then unique naming.

    The service knows nothing about PDFs. It resolves an extractor for the upload, drains
    its per-page results, and applies the document-wide naming pass. Adding a new source
    kind therefore requires no change here.
    """

    def __init__(self, registry: ExtractorRegistry, *, max_upload_bytes: int) -> None:
        """Initialise the service.

        Args:
            registry: Resolves the extractor for an uploaded document.
            max_upload_bytes: Reject uploads larger than this.
        """
        self._registry = registry
        self._max_upload_bytes = max_upload_bytes

    def _validate(self, request: ExtractionRequest) -> None:
        """Reject uploads that are empty or too large.

        Raises:
            EmptyDocumentError: If the upload has no content.
            DocumentTooLargeError: If the upload exceeds the configured limit.
        """
        size = len(request.content)
        if size == 0:
            raise EmptyDocumentError
        if size > self._max_upload_bytes:
            raise DocumentTooLargeError(size, self._max_upload_bytes)

    async def extract(self, request: ExtractionRequest) -> ExtractionResponse:
        """Extract all form fields from a document.

        Args:
            request: The uploaded document.

        Returns:
            The completed field inventory with unique names.

        Raises:
            EmptyDocumentError: If the upload has no content.
            DocumentTooLargeError: If the upload is too large.
            UnsupportedSourceError: If no extractor handles the media type.
        """
        response: ExtractionResponse | None = None
        async for event in self.extract_streaming(request):
            if isinstance(event, ExtractionResponse):
                response = event

        if response is None:  # pragma: no cover - the generator always ends with a response
            raise RuntimeError("Extraction finished without producing a result")
        return response

    async def extract_streaming(self, request: ExtractionRequest) -> AsyncIterator[ExtractionEvent]:
        """Extract form fields, reporting progress as each page completes.

        Args:
            request: The uploaded document.

        Yields:
            An `ExtractionProgress` after every page, then exactly one `ExtractionResponse`.

        Raises:
            EmptyDocumentError: If the upload has no content.
            DocumentTooLargeError: If the upload is too large.
            UnsupportedSourceError: If no extractor handles the media type.
        """
        self._validate(request)
        extractor = self._registry.resolve(request.media_type)

        logger.info(
            "extraction_started",
            filename=request.filename,
            media_type=request.media_type,
            source_kind=extractor.source_kind,
            size_bytes=len(request.content),
        )

        collected: list[ExtractedField] = []
        warnings: list[str] = []
        page_count = 0

        async for page_result in extractor.extract(request):
            collected.extend(page_result.fields)
            warnings.extend(page_result.warnings)
            page_count = max(page_count, page_result.page)
            yield ExtractionProgress(
                page=page_result.page,
                total_pages=page_result.total_pages,
                fields_found=len(collected),
            )

        fields, naming_warnings = ensure_unique_names(collected)
        warnings.extend(naming_warnings)

        logger.info(
            "extraction_finished",
            filename=request.filename,
            pages=page_count,
            fields=len(fields),
            warnings=len(warnings),
        )

        yield ExtractionResponse(
            source_name=request.filename,
            source_kind=extractor.source_kind,
            page_count=page_count,
            fields=fields,
            warnings=warnings,
        )

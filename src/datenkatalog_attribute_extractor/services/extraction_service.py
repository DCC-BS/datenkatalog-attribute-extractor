"""Source-agnostic orchestration of a field extraction run."""

import base64
from collections.abc import AsyncIterator

from dcc_backend_common.logger import get_logger

from datenkatalog_attribute_extractor.models.extraction import (
    ExtractionProgress,
    ExtractionResponse,
    ExtractionSource,
    PageImage,
    UploadSource,
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

    def _validate(self, source: ExtractionSource) -> None:
        """Reject uploads that are empty or too large.

        The size limit guards the bytes a caller pushed at us, so it applies to uploads only.
        A URL costs nothing to accept here; what it may fetch is bounded by the extractor.

        Raises:
            EmptyDocumentError: If the upload has no content.
            DocumentTooLargeError: If the upload exceeds the configured limit.
        """
        if not isinstance(source, UploadSource):
            return

        size = len(source.content)
        if size == 0:
            raise EmptyDocumentError
        if size > self._max_upload_bytes:
            raise DocumentTooLargeError(size, self._max_upload_bytes)

    async def extract(self, source: ExtractionSource) -> ExtractionResponse:
        """Extract all form fields from a source.

        Args:
            source: The uploaded document or URL to read.

        Returns:
            The completed field inventory with unique names.

        Raises:
            EmptyDocumentError: If the upload has no content.
            DocumentTooLargeError: If the upload is too large.
            UnsupportedSourceError: If no extractor handles the source.
        """
        response: ExtractionResponse | None = None
        async for event in self.extract_streaming(source):
            if isinstance(event, ExtractionResponse):
                response = event

        if response is None:  # pragma: no cover - the generator always ends with a response
            raise RuntimeError("Extraction finished without producing a result")
        return response

    async def extract_streaming(self, source: ExtractionSource) -> AsyncIterator[ExtractionEvent]:
        """Extract form fields, reporting progress as each unit of work completes.

        Args:
            source: The uploaded document or URL to read.

        Yields:
            An `ExtractionProgress` after every page, then exactly one `ExtractionResponse`.

        Raises:
            EmptyDocumentError: If the upload has no content.
            DocumentTooLargeError: If the upload is too large.
            UnsupportedSourceError: If no extractor handles the source.
        """
        self._validate(source)
        extractor = self._registry.resolve(source)

        logger.info(
            "extraction_started",
            source_name=source.name,
            source_kind=extractor.source_kind,
            size_bytes=len(source.content) if isinstance(source, UploadSource) else None,
        )

        collected: list[ExtractedField] = []
        warnings: list[str] = []
        images: list[PageImage] = []
        page_count = 0

        async for page_result in extractor.extract(source):
            collected.extend(page_result.fields)
            warnings.extend(page_result.warnings)
            page_count = max(page_count, page_result.page)
            images.extend(
                PageImage(page=page_result.page, image_base64=base64.b64encode(image).decode())
                for image in page_result.images
            )
            yield ExtractionProgress(
                page=page_result.page,
                total_pages=page_result.total_pages,
                fields_found=len(collected),
            )

        fields, naming_warnings = ensure_unique_names(collected)
        warnings.extend(naming_warnings)

        logger.info(
            "extraction_finished",
            source_name=source.name,
            pages=page_count,
            fields=len(fields),
            warnings=len(warnings),
        )

        yield ExtractionResponse(
            source_name=source.name,
            source_kind=extractor.source_kind,
            page_count=page_count,
            fields=fields,
            warnings=warnings,
            page_images=images,
        )

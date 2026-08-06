"""HTTP surface for form field extraction."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

from dcc_backend_common.fastapi_error_handling import ApiErrorCodes, api_error_exception
from dcc_backend_common.logger import get_logger
from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, File, UploadFile, status
from fastapi.responses import StreamingResponse

from datenkatalog_attribute_extractor.container import Container
from datenkatalog_attribute_extractor.models.extraction import (
    ExtractionProgress,
    ExtractionResponse,
    ExtractionSource,
    UploadSource,
    UrlExtractionRequest,
    UrlSource,
)
from datenkatalog_attribute_extractor.services.extraction_service import (
    DocumentTooLargeError,
    EmptyDocumentError,
    ExtractionService,
)
from datenkatalog_attribute_extractor.services.extractors.protocol import UnsupportedSourceError
from datenkatalog_attribute_extractor.services.llm_health import LlmUnavailableError
from datenkatalog_attribute_extractor.services.web.browser_client import (
    BrowserUnavailableError,
    PageUnreadableError,
)
from datenkatalog_attribute_extractor.services.web.url_policy import UnsafeUrlError
from datenkatalog_attribute_extractor.utils.sse import format_sse

logger = get_logger(__name__)

OCTET_STREAM = "application/octet-stream"
EXTENSION_MEDIA_TYPES = {".pdf": "application/pdf"}

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def resolve_media_type(upload: UploadFile) -> str:
    """Determine the media type of an upload.

    Clients frequently send `application/octet-stream`, so the filename extension is used
    as a fallback.

    Args:
        upload: The uploaded file.

    Returns:
        The resolved media type.
    """
    declared = (upload.content_type or "").split(";")[0].strip().lower()
    if declared and declared != OCTET_STREAM:
        return declared

    suffix = Path(upload.filename or "").suffix.lower()
    return EXTENSION_MEDIA_TYPES.get(suffix, declared or OCTET_STREAM)


async def build_upload_source(upload: UploadFile) -> UploadSource:
    """Read an upload into an extraction source.

    Args:
        upload: The uploaded file.

    Returns:
        The source to hand to the extraction service.
    """
    content = await upload.read()
    return UploadSource(
        content=content,
        filename=upload.filename or "unnamed",
        media_type=resolve_media_type(upload),
    )


def to_api_error(error: Exception) -> Exception:
    """Translate a domain error into the standard API error response.

    Args:
        error: The domain error raised by the extraction service.

    Returns:
        The `ApiErrorException` to raise instead.
    """
    if isinstance(error, UnsupportedSourceError):
        return api_error_exception(
            errorId=ApiErrorCodes.INVALID_REQUEST,
            status=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            debugMessage=str(error),
        )
    # A rejected URL and a page that will not load are both the caller's problem, not ours:
    # they describe the request, so they must not read as a service outage.
    if isinstance(error, DocumentTooLargeError | EmptyDocumentError | UnsafeUrlError | PageUnreadableError):
        return api_error_exception(
            errorId=ApiErrorCodes.INVALID_REQUEST,
            status=status.HTTP_400_BAD_REQUEST,
            debugMessage=str(error),
        )
    if isinstance(error, LlmUnavailableError | BrowserUnavailableError):
        return api_error_exception(
            errorId=ApiErrorCodes.SERVICE_UNAVAILABLE,
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
            debugMessage=str(error),
        )
    return api_error_exception(
        errorId=ApiErrorCodes.UNEXPECTED_ERROR,
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        debugMessage=str(error),
    )


@inject
def create_router(
    extraction_service: ExtractionService = Provide[Container.extraction_service],
) -> APIRouter:
    """Build the extraction router.

    Args:
        extraction_service: The orchestrator, injected from the container.

    Returns:
        The configured router.
    """
    router = APIRouter(prefix="/extraction", tags=["extraction"])

    @router.post("/form-fields", response_model=ExtractionResponse)
    async def extract_form_fields(file: UploadFile = File(...)) -> ExtractionResponse:  # noqa: B008
        """Extract every form field of an uploaded document.

        Runs to completion before responding. For documents of more than a few pages,
        prefer the streaming endpoint so the caller sees progress.
        """
        source = await build_upload_source(file)
        try:
            return await extraction_service.extract(source)
        except Exception as error:
            raise to_api_error(error) from error

    @router.post("/form-fields/stream")
    async def extract_form_fields_streaming(file: UploadFile = File(...)) -> StreamingResponse:  # noqa: B008
        """Extract form fields, streaming progress as server-sent events.

        Emits a `progress` event per completed page, then a single `result` event. Failures
        that occur once streaming has begun are reported as an `error` event, because the
        HTTP status has already been sent.
        """
        source = await build_upload_source(file)
        return stream_extraction(extraction_service, source)

    @router.post("/form-fields/url", response_model=ExtractionResponse)
    async def extract_form_fields_from_url(request: UrlExtractionRequest) -> ExtractionResponse:
        """Extract every form field of an online form.

        The page is fetched once. A form split across several pages is not followed; where the
        page looks like one step of several, the response carries a warning saying so.
        """
        try:
            return await extraction_service.extract(
                UrlSource(url=str(request.url), force_screenshots=request.force_screenshots)
            )
        except Exception as error:
            raise to_api_error(error) from error

    @router.post("/form-fields/url/stream")
    async def extract_form_fields_from_url_streaming(request: UrlExtractionRequest) -> StreamingResponse:
        """Extract form fields from an online form, streaming progress as server-sent events.

        Emits the same `progress`, `result` and `error` events as the upload endpoint, so a
        client needs no separate handling for the two kinds of source.
        """
        return stream_extraction(
            extraction_service,
            UrlSource(url=str(request.url), force_screenshots=request.force_screenshots),
        )

    return router


def stream_extraction(extraction_service: ExtractionService, source: ExtractionSource) -> StreamingResponse:
    """Run an extraction and report it as server-sent events.

    Shared by the upload and URL endpoints: the event contract does not depend on where the
    fields came from, and neither does what a mid-stream failure has to look like.

    Args:
        extraction_service: The orchestrator.
        source: The document or URL to read.

    Returns:
        A streaming response emitting `progress` events, then one `result` or `error` event.
    """

    async def generate() -> AsyncIterator[str]:
        try:
            async for event in extraction_service.extract_streaming(source):
                if isinstance(event, ExtractionProgress):
                    yield format_sse("progress", event.model_dump_json())
                else:
                    yield format_sse("result", event.model_dump_json())
        except asyncio.CancelledError:
            logger.info("extraction_cancelled", source_name=source.name)
            raise
        except Exception as error:
            logger.exception("extraction_stream_failed", source_name=source.name)
            api_error = to_api_error(error)
            payload = getattr(api_error, "error_response", {"debugMessage": str(error)})
            yield format_sse("error", json.dumps(payload))

    return StreamingResponse(generate(), media_type="text/event-stream", headers=SSE_HEADERS)

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
    ExtractionRequest,
    ExtractionResponse,
)
from datenkatalog_attribute_extractor.services.extraction_service import (
    DocumentTooLargeError,
    EmptyDocumentError,
    ExtractionService,
)
from datenkatalog_attribute_extractor.services.extractors.protocol import UnsupportedSourceError
from datenkatalog_attribute_extractor.services.llm_health import LlmUnavailableError
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


async def build_request(upload: UploadFile) -> ExtractionRequest:
    """Read an upload into an extraction request.

    Args:
        upload: The uploaded file.

    Returns:
        The request to hand to the extraction service.
    """
    content = await upload.read()
    return ExtractionRequest(
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
    if isinstance(error, DocumentTooLargeError | EmptyDocumentError):
        return api_error_exception(
            errorId=ApiErrorCodes.INVALID_REQUEST,
            status=status.HTTP_400_BAD_REQUEST,
            debugMessage=str(error),
        )
    if isinstance(error, LlmUnavailableError):
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
        request = await build_request(file)
        try:
            return await extraction_service.extract(request)
        except Exception as error:
            raise to_api_error(error) from error

    @router.post("/form-fields/stream")
    async def extract_form_fields_streaming(file: UploadFile = File(...)) -> StreamingResponse:  # noqa: B008
        """Extract form fields, streaming progress as server-sent events.

        Emits a `progress` event per completed page, then a single `result` event. Failures
        that occur once streaming has begun are reported as an `error` event, because the
        HTTP status has already been sent.
        """
        request = await build_request(file)

        async def generate() -> AsyncIterator[str]:
            try:
                async for event in extraction_service.extract_streaming(request):
                    if isinstance(event, ExtractionProgress):
                        yield format_sse("progress", event.model_dump_json())
                    else:
                        yield format_sse("result", event.model_dump_json())
            except asyncio.CancelledError:
                logger.info("extraction_cancelled", filename=request.filename)
                raise
            except Exception as error:
                logger.exception("extraction_stream_failed", filename=request.filename)
                api_error = to_api_error(error)
                payload = getattr(api_error, "error_response", {"debugMessage": str(error)})
                yield format_sse("error", json.dumps(payload))

        return StreamingResponse(generate(), media_type="text/event-stream", headers=SSE_HEADERS)

    return router

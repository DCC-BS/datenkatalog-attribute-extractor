"""Tests for the source-agnostic extraction orchestrator."""

from collections.abc import AsyncIterator

import pytest

from datenkatalog_attribute_extractor.models.enums import SourceKind
from datenkatalog_attribute_extractor.models.extraction import (
    ExtractionProgress,
    ExtractionRequest,
    ExtractionResponse,
    PageResult,
)
from datenkatalog_attribute_extractor.services.extraction_service import (
    DocumentTooLargeError,
    EmptyDocumentError,
    ExtractionService,
)
from datenkatalog_attribute_extractor.services.extractors.protocol import (
    ExtractorRegistry,
    UnsupportedSourceError,
)
from tests.factories import make_field


class StubExtractor:
    """A real extractor that replays canned page results, so no LLM is involved."""

    source_kind = SourceKind.PDF

    def __init__(self, pages: list[PageResult], media_type: str = "application/pdf") -> None:
        self._pages = pages
        self._media_type = media_type

    def supports(self, media_type: str) -> bool:
        return media_type == self._media_type

    async def extract(self, request: ExtractionRequest) -> AsyncIterator[PageResult]:
        for page in self._pages:
            yield page


def make_request(content: bytes = b"%PDF-1.5 fake", media_type: str = "application/pdf") -> ExtractionRequest:
    """Build an extraction request for tests."""
    return ExtractionRequest(content=content, filename="form.pdf", media_type=media_type)


def make_service(pages: list[PageResult], *, max_upload_bytes: int = 1_000_000) -> ExtractionService:
    """Build a service backed by a stub extractor."""
    registry = ExtractorRegistry([StubExtractor(pages)])
    return ExtractionService(registry, max_upload_bytes=max_upload_bytes)


TWO_PAGES = [
    PageResult(
        page=1,
        total_pages=2,
        fields=[
            make_field(label="Familienname:", context_path=["Vertreter", "Vater"], page=1),
            make_field(label="Familienname:", context_path=["Vertreter", "Mutter"], page=1),
        ],
        warnings=[],
    ),
    PageResult(
        page=2,
        total_pages=2,
        fields=[make_field(label="Unterschrift:", context_path=[], page=2)],
        warnings=["No fields were found on page 2"],
    ),
]


async def test_extract_returns_uniquely_named_fields_from_all_pages() -> None:
    response = await make_service(TWO_PAGES).extract(make_request())

    assert response.page_count == 2
    assert [field.name for field in response.fields] == [
        "vater_familienname",
        "mutter_familienname",
        "unterschrift",
    ]
    assert response.source_kind is SourceKind.PDF
    assert response.source_name == "form.pdf"


async def test_extract_collects_extractor_and_naming_warnings() -> None:
    response = await make_service(TWO_PAGES).extract(make_request())

    assert "No fields were found on page 2" in response.warnings
    assert any("Familienname" in warning for warning in response.warnings)


async def test_extract_streaming_emits_progress_per_page_then_one_result() -> None:
    events = [event async for event in make_service(TWO_PAGES).extract_streaming(make_request())]

    progress = [event for event in events if isinstance(event, ExtractionProgress)]
    results = [event for event in events if isinstance(event, ExtractionResponse)]

    assert [(item.page, item.total_pages, item.fields_found) for item in progress] == [(1, 2, 2), (2, 2, 3)]
    assert len(results) == 1
    assert isinstance(events[-1], ExtractionResponse)


async def test_extract_with_empty_document_raises_empty_document_error() -> None:
    with pytest.raises(EmptyDocumentError):
        await make_service(TWO_PAGES).extract(make_request(content=b""))


async def test_extract_with_oversized_document_raises_too_large_error() -> None:
    service = make_service(TWO_PAGES, max_upload_bytes=4)

    with pytest.raises(DocumentTooLargeError):
        await service.extract(make_request(content=b"much too long"))


async def test_extract_with_unsupported_media_type_raises_unsupported_source_error() -> None:
    with pytest.raises(UnsupportedSourceError):
        await make_service(TWO_PAGES).extract(make_request(media_type="text/plain"))


async def test_extract_with_a_document_yielding_no_fields_returns_an_empty_inventory() -> None:
    pages = [PageResult(page=1, total_pages=1, fields=[], warnings=["No fields were found on page 1"])]

    response = await make_service(pages).extract(make_request())

    assert response.fields == []
    assert response.page_count == 1

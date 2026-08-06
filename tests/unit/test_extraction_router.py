"""Tests for the extraction HTTP surface.

`create_router` takes its service as a parameter whose default is a DI marker, so passing a
stub explicitly bypasses the container and needs no environment.
"""

from collections.abc import AsyncIterator

from dcc_backend_common.fastapi_error_handling import inject_api_error_handler
from fastapi import FastAPI
from fastapi.testclient import TestClient

from datenkatalog_attribute_extractor.models.enums import SourceKind
from datenkatalog_attribute_extractor.models.extraction import (
    ExtractionSource,
    PageResult,
    UploadSource,
    UrlSource,
)
from datenkatalog_attribute_extractor.routers.extraction import create_router, resolve_media_type
from datenkatalog_attribute_extractor.services.extraction_service import ExtractionService
from datenkatalog_attribute_extractor.services.extractors.protocol import ExtractorRegistry
from datenkatalog_attribute_extractor.services.llm_health import LlmUnavailableError
from datenkatalog_attribute_extractor.services.web.browser_client import (
    BrowserUnavailableError,
    PageUnreadableError,
)
from datenkatalog_attribute_extractor.services.web.url_policy import UnsafeUrlError
from datenkatalog_attribute_extractor.utils.sse import parse_sse
from tests.factories import make_field
from tests.unit.test_extraction_service import StubExtractor

PAGES = [
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
        warnings=[],
    ),
]


class UnavailableLlmExtractor:
    """An extractor that reports the LLM as unusable, as the PDF extractor does."""

    source_kind = SourceKind.PDF

    def supports(self, source: ExtractionSource) -> bool:
        return isinstance(source, UploadSource) and source.media_type == "application/pdf"

    async def extract(self, source: ExtractionSource) -> AsyncIterator[PageResult]:
        raise LlmUnavailableError("http://llm:8000/health", "Connection refused")
        yield  # pragma: no cover - makes this an async generator


def build_client(
    pages: list[PageResult] | None = None,
    *,
    max_upload_bytes: int = 1_000_000,
    extractor: object | None = None,
) -> TestClient:
    """Build a test client over a router backed by a stub extractor."""
    chosen = extractor if extractor is not None else StubExtractor(pages if pages is not None else PAGES)
    registry = ExtractorRegistry([chosen])  # ty: ignore[invalid-argument-type]
    service = ExtractionService(registry, max_upload_bytes=max_upload_bytes)

    app = FastAPI()
    app.include_router(create_router(extraction_service=service))
    inject_api_error_handler(app)
    return TestClient(app)


def pdf_upload(content: bytes = b"%PDF-1.5 fake", media_type: str = "application/pdf") -> dict[str, tuple]:
    """Build a multipart payload for an upload."""
    return {"file": ("form.pdf", content, media_type)}


def test_extract_form_fields_returns_uniquely_named_fields() -> None:
    response = build_client().post("/extraction/form-fields", files=pdf_upload())

    assert response.status_code == 200
    payload = response.json()
    assert [field["name"] for field in payload["fields"]] == [
        "vater_familienname",
        "mutter_familienname",
        "unterschrift",
    ]
    assert payload["source_kind"] == "pdf"
    assert payload["page_count"] == 2


def test_extract_form_fields_rejects_a_non_pdf_upload() -> None:
    response = build_client().post("/extraction/form-fields", files=pdf_upload(media_type="text/plain"))

    assert response.status_code == 415
    assert response.json()["errorId"] == "invalid_request"


def test_extract_form_fields_rejects_an_oversized_upload() -> None:
    client = build_client(max_upload_bytes=4)

    response = client.post("/extraction/form-fields", files=pdf_upload(content=b"far too many bytes"))

    assert response.status_code == 400


def test_extract_form_fields_rejects_an_empty_upload() -> None:
    response = build_client().post("/extraction/form-fields", files=pdf_upload(content=b""))

    assert response.status_code == 400


def test_streaming_endpoint_emits_progress_events_then_a_result() -> None:
    response = build_client().post("/extraction/form-fields/stream", files=pdf_upload())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = list(parse_sse(response.text.split("\n")))
    names = [name for name, _ in events]

    assert names == ["progress", "progress", "result"]
    assert [payload["page"] for _, payload in events[:2]] == [1, 2]
    assert [payload["fields_found"] for _, payload in events[:2]] == [2, 3]

    _, result = events[-1]
    assert [field["name"] for field in result["fields"]] == [
        "vater_familienname",
        "mutter_familienname",
        "unterschrift",
    ]


def test_streaming_endpoint_reports_a_rejected_upload_as_an_error_event() -> None:
    response = build_client().post("/extraction/form-fields/stream", files=pdf_upload(content=b""))

    events = list(parse_sse(response.text.split("\n")))

    assert [name for name, _ in events] == ["error"]
    assert events[0][1]["status"] == 400


def test_extract_form_fields_returns_503_when_the_llm_is_unavailable() -> None:
    client = build_client(extractor=UnavailableLlmExtractor())

    response = client.post("/extraction/form-fields", files=pdf_upload())

    assert response.status_code == 503
    payload = response.json()
    assert payload["errorId"] == "service_unavailable"
    assert "unavailable" in payload["debugMessage"]


def test_streaming_endpoint_reports_llm_unavailability_as_an_error_event() -> None:
    client = build_client(extractor=UnavailableLlmExtractor())

    response = client.post("/extraction/form-fields/stream", files=pdf_upload())

    events = list(parse_sse(response.text.split("\n")))

    assert [name for name, _ in events] == ["error"]
    assert events[0][1]["status"] == 503
    assert events[0][1]["errorId"] == "service_unavailable"


def test_resolve_media_type_prefers_the_declared_content_type() -> None:
    class Upload:
        content_type = "application/pdf"
        filename = "form.bin"

    assert resolve_media_type(Upload()) == "application/pdf"


def test_resolve_media_type_falls_back_to_the_filename_extension() -> None:
    class Upload:
        content_type = "application/octet-stream"
        filename = "form.PDF"

    assert resolve_media_type(Upload()) == "application/pdf"


def test_resolve_media_type_strips_parameters_from_the_content_type() -> None:
    class Upload:
        content_type = "application/pdf; charset=binary"
        filename = "form.pdf"

    assert resolve_media_type(Upload()) == "application/pdf"


class StubWebExtractor:
    """A web extractor that replays canned results, or raises."""

    source_kind = SourceKind.WEB

    def __init__(self, pages: list[PageResult] | None = None, error: Exception | None = None) -> None:
        self._pages = pages if pages is not None else PAGES
        self._error = error
        self.sources: list[UrlSource] = []

    def supports(self, source: ExtractionSource) -> bool:
        return isinstance(source, UrlSource)

    async def extract(self, source: ExtractionSource) -> AsyncIterator[PageResult]:
        assert isinstance(source, UrlSource)
        self.sources.append(source)
        if self._error is not None:
            raise self._error
        for page in self._pages:
            yield page


FORM_URL = "https://example.org/anmeldung"


def test_extract_from_url_returns_uniquely_named_fields() -> None:
    client = build_client(extractor=StubWebExtractor())

    response = client.post("/extraction/form-fields/url", json={"url": FORM_URL})

    assert response.status_code == 200
    body = response.json()
    assert body["source_kind"] == "web"
    assert body["source_name"] == FORM_URL
    names = [field["name"] for field in body["fields"]]
    assert len(names) == len(set(names))


def test_extract_from_url_passes_the_screenshot_request_through() -> None:
    """The reviewer's override has to survive the whole way to the extractor."""
    extractor = StubWebExtractor()
    client = build_client(extractor=extractor)

    client.post("/extraction/form-fields/url", json={"url": FORM_URL, "force_screenshots": True})

    assert [source.force_screenshots for source in extractor.sources] == [True]


def test_extract_from_url_reads_the_controls_by_default() -> None:
    extractor = StubWebExtractor()
    client = build_client(extractor=extractor)

    client.post("/extraction/form-fields/url", json={"url": FORM_URL})

    assert [source.force_screenshots for source in extractor.sources] == [False]


def test_extract_from_url_rejects_a_malformed_url() -> None:
    client = build_client(extractor=StubWebExtractor())

    response = client.post("/extraction/form-fields/url", json={"url": "not a url"})

    assert response.status_code == 422


def test_extract_from_url_reports_an_unsafe_url_as_a_client_error() -> None:
    """An internal address is the caller's mistake, not an outage."""
    error = UnsafeUrlError("http://127.0.0.1/x", "the host resolves to the internal address 127.0.0.1")
    client = build_client(extractor=StubWebExtractor(error=error))

    response = client.post("/extraction/form-fields/url", json={"url": FORM_URL})

    assert response.status_code == 400


def test_extract_from_url_reports_an_unreadable_page_as_a_client_error() -> None:
    error = PageUnreadableError(FORM_URL, "it answered HTTP 404 (Not Found)")
    client = build_client(extractor=StubWebExtractor(error=error))

    response = client.post("/extraction/form-fields/url", json={"url": FORM_URL})

    assert response.status_code == 400


def test_extract_from_url_reports_the_browser_being_down_as_unavailable() -> None:
    error = BrowserUnavailableError("http://browser:3100/observe", "ConnectTimeout")
    client = build_client(extractor=StubWebExtractor(error=error))

    response = client.post("/extraction/form-fields/url", json={"url": FORM_URL})

    assert response.status_code == 503


def test_streaming_from_url_emits_progress_then_a_result() -> None:
    client = build_client(extractor=StubWebExtractor())

    with client.stream("POST", "/extraction/form-fields/url/stream", json={"url": FORM_URL}) as response:
        assert response.status_code == 200
        events = list(parse_sse(response.iter_lines()))

    kinds = [event for event, _ in events]
    assert kinds.count("progress") == len(PAGES)
    assert kinds[-1] == "result"


def test_streaming_from_url_reports_a_mid_stream_failure_as_an_error_event() -> None:
    """The HTTP status is already sent by then, so the failure has to travel in the stream."""
    error = BrowserUnavailableError("http://browser:3100/observe", "ConnectTimeout")
    client = build_client(extractor=StubWebExtractor(error=error))

    with client.stream("POST", "/extraction/form-fields/url/stream", json={"url": FORM_URL}) as response:
        assert response.status_code == 200
        events = list(parse_sse(response.iter_lines()))

    assert [event for event, _ in events] == ["error"]


def test_a_url_source_does_not_reach_the_pdf_extractor() -> None:
    client = build_client()

    response = client.post("/extraction/form-fields/url", json={"url": FORM_URL})

    assert response.status_code == 415

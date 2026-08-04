"""Tests that an unusable LLM aborts a run instead of being absorbed page by page."""

from pathlib import Path

import httpx
import openai
import pytest
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError

from datenkatalog_attribute_extractor.models.extraction import UploadSource
from datenkatalog_attribute_extractor.models.field import PageExtraction
from datenkatalog_attribute_extractor.services.extraction_service import ExtractionService
from datenkatalog_attribute_extractor.services.extractors.pdf_extractor import PdfFieldExtractor
from datenkatalog_attribute_extractor.services.extractors.protocol import ExtractorRegistry
from datenkatalog_attribute_extractor.services.llm_health import LlmHealthProbe, LlmUnavailableError
from tests.factories import make_field

EXAMPLE_PDF = Path(__file__).resolve().parents[2] / "data" / "example.pdf"
HEALTH_URL = "http://llm:8000/health"


class CountingAgent:
    """Stands in for the vision agent, recording how often it was called."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self._error = error

    async def extract_page(self, png_bytes: bytes) -> PageExtraction:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return PageExtraction(fields=[make_field(label="Name:")])


MODEL_NAME = "Gemma/Gemma-4-31B"


def make_probe(healthy: bool) -> LlmHealthProbe:
    """A probe wired to a mock transport that reports the LLM up or down."""

    def handler(request: httpx.Request) -> httpx.Response:
        if not healthy:
            raise httpx.ConnectError("Connection refused")
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": MODEL_NAME, "max_model_len": 16384}]})
        return httpx.Response(200)

    return LlmHealthProbe(
        health_check_url=HEALTH_URL,
        models_url="http://llm:8000/v1/models",
        model_name=MODEL_NAME,
        api_key="",
        timeout_seconds=1,
        min_context_tokens=8192,
        transport=httpx.MockTransport(handler),
    )


def make_extractor(agent: CountingAgent, *, healthy: bool) -> PdfFieldExtractor:
    """Build the real extractor over a stub agent and a mocked health probe."""
    return PdfFieldExtractor(
        agent,  # ty: ignore[invalid-argument-type]
        make_probe(healthy),
        render_dpi=72,
        max_pages=30,
        max_concurrency=1,
    )


def pdf_request() -> UploadSource:
    """The bundled 7-page example as an upload source."""
    return UploadSource(
        content=EXAMPLE_PDF.read_bytes(),
        filename="example.pdf",
        media_type="application/pdf",
    )


async def drain(extractor: PdfFieldExtractor) -> list:
    """Consume every page result the extractor yields."""
    return [page async for page in extractor.extract(pdf_request())]


async def test_extract_aborts_before_rendering_when_the_llm_is_down() -> None:
    agent = CountingAgent()
    extractor = make_extractor(agent, healthy=False)

    with pytest.raises(LlmUnavailableError):
        await drain(extractor)

    assert agent.calls == 0, "no page should have been sent to a dead LLM"


async def test_extract_proceeds_normally_when_the_llm_is_healthy() -> None:
    agent = CountingAgent()

    pages = await drain(make_extractor(agent, healthy=True))

    assert len(pages) == 7
    assert agent.calls == 7


def wrapped_connection_error() -> ModelAPIError:
    """The exception the agent really raises: pydantic-ai's wrapper around an openai error."""
    error = ModelAPIError(model_name="gemma", message="Connection error.")
    error.__cause__ = openai.APIConnectionError(request=httpx.Request("POST", "http://llm/v1"))
    return error


async def test_a_fatal_error_mid_run_aborts_instead_of_warning_on_every_page() -> None:
    """A connection dropping after page one must not produce seven identical warnings."""
    agent = CountingAgent(wrapped_connection_error())
    extractor = make_extractor(agent, healthy=True)

    with pytest.raises(LlmUnavailableError):
        await drain(extractor)

    assert agent.calls == 1, "the run should stop at the first fatal failure"


async def test_a_raw_provider_error_also_aborts_the_run() -> None:
    """Defensive: the same holds if an unwrapped provider error ever reaches us."""
    agent = CountingAgent(openai.APIConnectionError(request=httpx.Request("POST", "http://llm/v1")))

    with pytest.raises(LlmUnavailableError):
        await drain(make_extractor(agent, healthy=True))

    assert agent.calls == 1


async def test_a_wrong_model_name_aborts_the_run() -> None:
    agent = CountingAgent(ModelHTTPError(status_code=404, model_name="nope", body="model not found"))

    with pytest.raises(LlmUnavailableError, match="unavailable"):
        await drain(make_extractor(agent, healthy=True))

    assert agent.calls == 1


async def test_a_page_local_failure_is_still_absorbed_as_a_warning() -> None:
    agent = CountingAgent(ValueError("could not parse the model output"))

    pages = await drain(make_extractor(agent, healthy=True))

    assert len(pages) == 7
    assert agent.calls == 7
    assert all(page.fields == [] for page in pages)
    assert all(any("could not be processed" in warning for warning in page.warnings) for page in pages)


async def test_the_service_surfaces_llm_unavailability_to_its_caller() -> None:
    agent = CountingAgent()
    registry = ExtractorRegistry([make_extractor(agent, healthy=False)])
    service = ExtractionService(registry, max_upload_bytes=10_000_000)

    with pytest.raises(LlmUnavailableError):
        await service.extract(pdf_request())

"""End-to-end extraction against a live Gemma 4 / vLLM instance.

Excluded from CI: it needs a running LLM and takes minutes. Run locally with

    PYTHONPATH=src uv run --env-file .env python -m pytest tests/integration -v -s
"""

import os
from pathlib import Path

import pytest

from datenkatalog_attribute_extractor.container import Container
from datenkatalog_attribute_extractor.models.extraction import ExtractionRequest

EXAMPLE_PDF = Path(__file__).resolve().parents[2] / "data" / "example.pdf"

pytestmark = pytest.mark.skipif(
    os.getenv("LLM_URL") is None,
    reason="LLM_URL is not set; start vLLM and load .env to run the integration tests",
)


@pytest.fixture(scope="module")
def service():
    """The fully wired extraction service."""
    return Container().extraction_service()


@pytest.fixture(scope="module")
def request_payload() -> ExtractionRequest:
    """The bundled example questionnaire as an extraction request."""
    return ExtractionRequest(
        content=EXAMPLE_PDF.read_bytes(),
        filename=EXAMPLE_PDF.name,
        media_type="application/pdf",
    )


@pytest.fixture(scope="module")
async def response(service, request_payload):
    """One extraction run, shared across the assertions below."""
    return await service.extract(request_payload)


async def test_extraction_processes_every_page(response) -> None:
    assert response.page_count == 7


async def test_extraction_finds_a_plausible_number_of_fields(response) -> None:
    assert len(response.fields) > 40


async def test_all_field_names_are_unique(response) -> None:
    names = [field.name for field in response.fields]

    assert len(set(names)) == len(names)


async def test_field_names_are_slugs(response) -> None:
    assert all(field.name.replace("_", "").isalnum() for field in response.fields)
    assert all(field.name == field.name.lower() for field in response.fields)


async def test_parent_columns_are_disambiguated_rather_than_collapsed(response) -> None:
    """The Vater/Mutter block on page 1 is the case the naming pass exists for."""
    names = {field.name for field in response.fields}
    family_names = {name for name in names if "familienname" in name}

    assert len(family_names) >= 2, f"expected separate father/mother fields, got {family_names}"


async def test_checkbox_groups_are_reported_once_not_per_option(response) -> None:
    """`Erziehungs- und Korrespondenzberechtigt` has four options but is one field."""
    matching = [field for field in response.fields if "korrespondenzberechtigt" in field.name]

    assert len(matching) <= 1, f"checkbox group was split into options: {[f.name for f in matching]}"

    options = {"beide_eltern", "nur_mutter", "nur_vater"}
    names = {field.name for field in response.fields}
    assert not options & names, "individual checkbox captions were emitted as fields"


async def test_fields_carry_page_numbers_in_range(response) -> None:
    assert all(1 <= field.page <= 7 for field in response.fields)


async def test_streaming_reports_progress_for_each_page(service, request_payload) -> None:
    from datenkatalog_attribute_extractor.models.extraction import ExtractionProgress, ExtractionResponse

    events = [event async for event in service.extract_streaming(request_payload)]
    progress = [event for event in events if isinstance(event, ExtractionProgress)]

    assert [item.page for item in progress] == list(range(1, 8))
    assert isinstance(events[-1], ExtractionResponse)

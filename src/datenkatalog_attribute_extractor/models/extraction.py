"""Request and response contracts for the extraction API."""

from dataclasses import dataclass

from pydantic import BaseModel, Field, HttpUrl

from datenkatalog_attribute_extractor.models.enums import SourceKind
from datenkatalog_attribute_extractor.models.field import ExtractedField, FormField


@dataclass(frozen=True, slots=True, kw_only=True)
class UploadSource:
    """A document uploaded as bytes, dispatched on its media type."""

    content: bytes
    filename: str
    media_type: str

    @property
    def name(self) -> str:
        """How this source is identified in responses and logs."""
        return self.filename


@dataclass(frozen=True, slots=True, kw_only=True)
class UrlSource:
    """A web page to be fetched and read, dispatched on being a URL at all.

    The page reaches the model as a compact listing of its form controls, derived from the
    DOM. A screenshot strategy was intended alongside it but is not buildable: self-hosted
    Firecrawl cannot capture screenshots, because its playwright engine neither requests one
    nor passes one through. See the module docstring of `services/web/firecrawl_client.py`.
    """

    url: str

    @property
    def name(self) -> str:
        """How this source is identified in responses and logs."""
        return self.url


type ExtractionSource = UploadSource | UrlSource
"""Everything a run can be started from.

Dispatch is on the source itself rather than a media type string, so a kind of source that
has no bytes and no media type — a URL, later an API endpoint — needs no special case in the
service or the registry.
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class PageResult:
    """The outcome of extracting a single page, yielded incrementally by an extractor."""

    page: int
    total_pages: int
    fields: list[ExtractedField]
    warnings: list[str]


class UrlExtractionRequest(BaseModel):
    """Body of the URL extraction endpoints."""

    url: HttpUrl = Field(description="Address of the online form to read")


class ExtractionProgress(BaseModel):
    """Progress of an in-flight extraction, emitted as an SSE `progress` event."""

    page: int = Field(description="1-based page that was just completed")
    total_pages: int = Field(description="Total number of pages being processed")
    fields_found: int = Field(description="Number of fields found so far across all completed pages")


class ExtractionResponse(BaseModel):
    """The completed field inventory of one document."""

    source_name: str = Field(description="Filename of the uploaded document, or the URL that was scraped")
    source_kind: SourceKind = Field(description="Which kind of source the fields were extracted from")
    page_count: int = Field(description="Number of units processed: PDF pages, HTML chunks or screenshot tiles")
    fields: list[FormField] = Field(description="All fields found, with document-wide unique names")
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal issues: renamed duplicates, empty pages, truncated model responses",
    )

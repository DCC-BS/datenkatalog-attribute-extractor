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

    The page is rendered in a browser and read either as a listing of its controls or, where
    that listing says too little, from screenshots of the same render. Which of the two was
    used is reported in the run's warnings; see `services/extractors/web_extractor.py`.
    """

    url: str
    force_screenshots: bool = False
    """Read the page from its screenshots whatever the control listing looks like.

    The automatic choice is made on how much of the listing carried a label, which is a good
    proxy and not a certainty: a page can hand every control a plausible caption and still have
    them wrong, and only a person looking at the form can tell. This is that person overruling
    the measurement.
    """

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
    image: bytes | None = None
    """The picture this unit was read from, PNG, where there was one.

    Only set where the reviewer has no other way of seeing what the model saw: a web page read
    from screenshots. A PDF page is not carried here — the client already holds the file and
    renders its own preview — and a listing chunk has no picture at all.
    """


class UrlExtractionRequest(BaseModel):
    """Body of the URL extraction endpoints."""

    url: HttpUrl = Field(description="Address of the online form to read")
    force_screenshots: bool = Field(
        default=False,
        description="Read the page from screenshots of the rendered form instead of from its controls",
    )


class ExtractionProgress(BaseModel):
    """Progress of an in-flight extraction, emitted as an SSE `progress` event."""

    page: int = Field(description="1-based page that was just completed")
    total_pages: int = Field(description="Total number of pages being processed")
    fields_found: int = Field(description="Number of fields found so far across all completed pages")


class PageImage(BaseModel):
    """The picture one unit of work was read from.

    Carried in the response so a reviewer sees the same screen the model did. A URL read from
    screenshots has no other preview: the live page in a browser is a fresh render, which is
    not necessarily the render the fields were read off.
    """

    page: int = Field(description="1-based unit the picture belongs to")
    image_base64: str = Field(description="The PNG the model read, base64-encoded")


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
    page_images: list[PageImage] = Field(
        default_factory=list,
        description="The pictures the units were read from, where the reading was made from pictures",
    )

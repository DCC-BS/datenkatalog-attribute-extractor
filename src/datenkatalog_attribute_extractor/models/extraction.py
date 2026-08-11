"""Request and response contracts for the extraction API."""

from dataclasses import (
    dataclass,
    field as dataclass_field,
)

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
    """A web form to be rendered and read, dispatched on being a URL at all.

    The form is rendered in a browser we control and read from screenshots of it, a few
    consecutive screens per call; see `services/extractors/web_extractor.py`.
    """

    url: str
    follow_steps: bool = True
    """Walk a multi-step form through its steps instead of reading only the first one.

    On by default, because the first step of a cantonal wizard is routinely a handful of fields
    out of eighty, and a partial inventory is the failure that looks most like a success.
    Walking means the browser answers the controls a step demands and presses that step's own
    *next* button — never anything that reads like a submit. Turning this off is for the case
    where even that is unwanted: a form whose steps have side effects, or a page being re-read
    quickly.
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
    images: list[bytes] = dataclass_field(default_factory=list)
    """The pictures this unit was read from, PNG, where there were any.

    A list because one call can carry several: a web form is read a few consecutive screens at
    a time, and all of them together are what the model was shown. Only set where the reviewer
    has no other way of seeing that — a PDF page is not carried here, since the client already
    holds the file and renders its own preview.
    """


class UrlExtractionRequest(BaseModel):
    """Body of the URL extraction endpoints."""

    url: HttpUrl = Field(description="Address of the online form to read")
    follow_steps: bool = Field(
        default=True,
        description="Follow a multi-step form through its steps instead of reading only the first one",
    )


class ExtractionProgress(BaseModel):
    """Progress of an in-flight extraction, emitted as an SSE `progress` event."""

    page: int = Field(description="1-based page that was just completed")
    total_pages: int = Field(description="Total number of pages being processed")
    fields_found: int = Field(description="Number of fields found so far across all completed pages")


class PageImage(BaseModel):
    """One picture a unit of work was read from.

    Carried in the response so a reviewer sees the same screens the model did. A web form has
    no other preview: the live page in a browser is a fresh render of its first step, which is
    not the render the fields were read off. Several entries may share a `page`, in the order
    they were shown, because one call is given several consecutive screens.
    """

    page: int = Field(description="1-based unit the picture belongs to")
    image_base64: str = Field(description="The PNG the model read, base64-encoded")


class ExtractionResponse(BaseModel):
    """The completed field inventory of one document."""

    source_name: str = Field(description="Filename of the uploaded document, or the URL that was scraped")
    source_kind: SourceKind = Field(description="Which kind of source the fields were extracted from")
    page_count: int = Field(description="Number of units processed: PDF pages, or calls over a web form's screens")
    fields: list[FormField] = Field(description="All fields found, with document-wide unique names")
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal issues: renamed duplicates, empty pages, truncated model responses",
    )
    page_images: list[PageImage] = Field(
        default_factory=list,
        description="The pictures the units were read from, in order; several may share a unit",
    )

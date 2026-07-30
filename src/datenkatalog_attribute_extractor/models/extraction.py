"""Request and response contracts for the extraction API."""

from dataclasses import dataclass

from pydantic import BaseModel, Field

from datenkatalog_attribute_extractor.models.enums import SourceKind
from datenkatalog_attribute_extractor.models.field import ExtractedField, FormField


@dataclass(frozen=True, slots=True, kw_only=True)
class ExtractionRequest:
    """An uploaded document to extract form fields from."""

    content: bytes
    filename: str
    media_type: str


@dataclass(frozen=True, slots=True, kw_only=True)
class PageResult:
    """The outcome of extracting a single page, yielded incrementally by an extractor."""

    page: int
    total_pages: int
    fields: list[ExtractedField]
    warnings: list[str]


class ExtractionProgress(BaseModel):
    """Progress of an in-flight extraction, emitted as an SSE `progress` event."""

    page: int = Field(description="1-based page that was just completed")
    total_pages: int = Field(description="Total number of pages being processed")
    fields_found: int = Field(description="Number of fields found so far across all completed pages")


class ExtractionResponse(BaseModel):
    """The completed field inventory of one document."""

    source_name: str = Field(description="Original filename of the uploaded document")
    source_kind: SourceKind = Field(description="Which kind of source the fields were extracted from")
    page_count: int = Field(description="Number of pages processed")
    fields: list[FormField] = Field(description="All fields found, with document-wide unique names")
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal issues: renamed duplicates, empty pages, truncated model responses",
    )

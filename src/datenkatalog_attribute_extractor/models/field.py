"""Form field contracts, from raw LLM output through to the named result."""

from pydantic import BaseModel, Field


class ExtractedField(BaseModel):
    """A single form field as read off one page, before a unique name is assigned.

    The model returns the label and its surrounding structure but never a name:
    uniqueness is a document-wide property that a page-scoped call cannot see.
    """

    label: str = Field(description="The field label exactly as printed in the document")
    context_path: list[str] = Field(
        default_factory=list,
        description="Enclosing headings from outermost to innermost, e.g. ['Gesetzliche Vertreter', 'Vater']",
    )
    page: int = Field(default=0, description="1-based page number, assigned by the extractor and not by the model")


class PageExtraction(BaseModel):
    """The structured output the extraction agent returns for a single page."""

    fields: list[ExtractedField] = Field(default_factory=list, description="Every input field found on this page")


class FormField(BaseModel):
    """A form field with a document-wide unique name."""

    name: str = Field(description="Unique snake_case identifier derived from the label")
    display_name: str = Field(
        description="Readable form of the same name, keeping original casing, spaces and umlauts",
    )
    label: str = Field(description="The field label exactly as printed in the document")
    context_path: list[str] = Field(default_factory=list, description="Enclosing headings, outermost first")
    page: int = Field(description="1-based page number the field was found on")

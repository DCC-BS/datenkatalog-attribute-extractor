"""Shared enumerations."""

from enum import StrEnum


class SourceKind(StrEnum):
    """The kind of document a set of form fields was extracted from."""

    PDF = "pdf"
    EXCEL = "excel"
    WEB = "web"

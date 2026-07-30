"""Reading AcroForm widget metadata from fillable PDFs.

Many of the questionnaires we process are fillable PDFs that already carry their own field
definitions. Those are useless at runtime — the service must also handle flat and scanned
documents — but they are free, independent ground truth for evaluating the vision pipeline.
"""

from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

FIELD_NAME_KEY = "/T"
FIELD_TOOLTIP_KEY = "/TU"
FIELD_TYPE_KEY = "/FT"

# Generated names carry no semantic information, so they are useless as expected labels.
GENERIC_NAME_PREFIXES = (
    "textfeld",
    "textfield",
    "text",
    "kontrollkästchen",
    "kontrollkaestchen",
    "checkbox",
    "check box",
    "optionsfeld",
    "radio",
    "feld",
    "field",
    "untitled",
)


@dataclass(frozen=True, slots=True, kw_only=True)
class AcroFormField:
    """One AcroForm widget as declared inside the PDF."""

    name: str
    tooltip: str | None
    field_type: str | None

    @property
    def best_label(self) -> str:
        """The most human-readable text available for this widget."""
        return self.tooltip.strip() if self.tooltip and self.tooltip.strip() else self.name


def read_acroform_fields(pdf_path: Path) -> list[AcroFormField]:
    """Read the AcroForm widgets declared in a PDF.

    Args:
        pdf_path: Path to the PDF file.

    Returns:
        One entry per declared widget, or an empty list for a flat PDF.
    """
    reader = PdfReader(str(pdf_path))
    raw_fields = reader.get_fields() or {}

    fields: list[AcroFormField] = []
    for key, value in raw_fields.items():
        name = str(value.get(FIELD_NAME_KEY, key) or key)
        tooltip = value.get(FIELD_TOOLTIP_KEY)
        field_type = value.get(FIELD_TYPE_KEY)
        fields.append(
            AcroFormField(
                name=name,
                tooltip=str(tooltip) if tooltip is not None else None,
                field_type=str(field_type) if field_type is not None else None,
            )
        )
    return fields


def is_generic_name(name: str) -> bool:
    """Report whether a widget name looks auto-generated rather than meaningful.

    Args:
        name: The widget's name.

    Returns:
        True if the name carries no semantic information.

    Example:
        >>> is_generic_name("Textfeld12")
        True
        >>> is_generic_name("Familienname Vater")
        False
    """
    normalised = name.strip().lower()
    stripped = normalised.rstrip("0123456789 _-.")
    return stripped in GENERIC_NAME_PREFIXES or not stripped

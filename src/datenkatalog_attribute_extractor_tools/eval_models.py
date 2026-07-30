"""Data model and scoring for the extraction eval harness."""

import json
from dataclasses import dataclass, field
from pathlib import Path

from datenkatalog_attribute_extractor.models.field import FormField
from datenkatalog_attribute_extractor.services.naming import slugify


@dataclass(frozen=True, slots=True, kw_only=True)
class EvalCase:
    """One labelled document to evaluate against.

    `expected_labels` is curated by hand. AcroForm widget names are only a starting point:
    in real questionnaires most of them are auto-generated (`Text1`, `Text2`, ...) and carry
    no meaning, so they cannot serve as expected labels on their own.
    """

    name: str
    pdf_path: Path
    expected_labels: list[str]
    expected_field_count: int | None = None
    notes: str = ""

    @classmethod
    def from_file(cls, path: Path) -> "EvalCase":
        """Load a case from its JSON definition.

        Args:
            path: Path to the case file.

        Returns:
            The parsed case, with `pdf_path` resolved relative to the repository root.
        """
        payload = json.loads(path.read_text(encoding="utf-8"))
        repo_root = path.resolve().parents[2]
        return cls(
            name=payload.get("name", path.stem),
            pdf_path=(repo_root / payload["pdf"]).resolve(),
            expected_labels=payload.get("expected_labels", []),
            expected_field_count=payload.get("expected_field_count"),
            notes=payload.get("notes", ""),
        )


@dataclass(slots=True, kw_only=True)
class CaseScore:
    """The outcome of evaluating one case."""

    name: str
    expected_count: int
    extracted_count: int
    total_fields: int = 0
    expected_field_count: int | None = None
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)
    duplicate_names: list[str] = field(default_factory=list)

    @property
    def recall(self) -> float:
        """Share of expected labels that were found."""
        return len(self.matched) / self.expected_count if self.expected_count else 0.0

    @property
    def precision(self) -> float:
        """Share of extracted fields that were expected."""
        return len(self.matched) / self.extracted_count if self.extracted_count else 0.0

    @property
    def f1(self) -> float:
        """Harmonic mean of precision and recall."""
        total = self.precision + self.recall
        return 2 * self.precision * self.recall / total if total else 0.0

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable summary."""
        return {
            "name": self.name,
            "expected_count": self.expected_count,
            "extracted_count": self.extracted_count,
            "total_fields": self.total_fields,
            "expected_field_count": self.expected_field_count,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "matched": self.matched,
            "missing": self.missing,
            "unexpected": self.unexpected,
            "duplicate_names": self.duplicate_names,
        }


def find_duplicate_names(fields: list[FormField]) -> list[str]:
    """Return any names that appear more than once.

    The naming pass guarantees uniqueness, so a non-empty result is a hard failure rather
    than a quality metric.

    Args:
        fields: The extracted fields.

    Returns:
        The duplicated names, sorted.
    """
    seen: set[str] = set()
    duplicates: set[str] = set()
    for item in fields:
        if item.name in seen:
            duplicates.add(item.name)
        seen.add(item.name)
    return sorted(duplicates)


def score_case(case: EvalCase, fields: list[FormField]) -> CaseScore:
    """Compare extracted fields against a case's expected labels.

    Matching is on slugified labels, so trailing colons and umlaut spelling do not affect
    the result. Precision and recall are computed over the *set* of distinct labels, because
    a label such as "Name:" legitimately occurs many times in one form. `total_fields` is
    reported alongside so that a run which finds every distinct label but only one of five
    "Name:" occurrences is still visible as a miss.

    Args:
        case: The labelled case.
        fields: The fields the pipeline extracted.

    Returns:
        The score for this case.
    """
    expected = {slugify(label) for label in case.expected_labels if slugify(label)}
    extracted = {slugify(item.label) for item in fields if slugify(item.label)}

    return CaseScore(
        name=case.name,
        expected_count=len(expected),
        extracted_count=len(extracted),
        total_fields=len(fields),
        expected_field_count=case.expected_field_count,
        matched=sorted(expected & extracted),
        missing=sorted(expected - extracted),
        unexpected=sorted(extracted - expected),
        duplicate_names=find_duplicate_names(fields),
    )

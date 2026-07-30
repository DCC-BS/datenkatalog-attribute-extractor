"""Generate an eval case skeleton from a PDF.

The skeleton is a starting point, not ground truth. AcroForm widget names in real
questionnaires are mostly auto-generated (`Text1`, `Text2`, ...) and each checkbox *option*
is its own widget, while our specification treats a checkbox group as a single field. The
generated `expected_labels` therefore always need curating by hand.

Usage:

    PYTHONPATH=src uv run python -m datenkatalog_attribute_extractor_tools.bootstrap_case data/example.pdf
"""

import argparse
import json
from pathlib import Path

from datenkatalog_attribute_extractor_tools.acroform import is_generic_name, read_acroform_fields

REPO_ROOT = Path(__file__).resolve().parents[2]
CASES_DIR = REPO_ROOT / "evals" / "cases"


def build_skeleton(pdf_path: Path) -> dict[str, object]:
    """Build a case skeleton for a PDF.

    Args:
        pdf_path: Path to the PDF.

    Returns:
        The case payload, ready to be written and then curated.
    """
    widgets = read_acroform_fields(pdf_path)
    usable = sorted({item.best_label for item in widgets if not is_generic_name(item.best_label)})
    generic_count = len(widgets) - len([item for item in widgets if not is_generic_name(item.best_label)])

    return {
        "name": pdf_path.stem,
        "pdf": str(pdf_path.relative_to(REPO_ROOT)) if pdf_path.is_relative_to(REPO_ROOT) else str(pdf_path),
        "notes": (
            f"Bootstrapped from {len(widgets)} AcroForm widgets; {generic_count} had generic names and were dropped. "
            "Checkbox options appear as individual widgets and must be collapsed into one group label by hand. "
            "CURATE expected_labels before trusting this case."
        ),
        "expected_labels": usable,
    }


def main() -> None:
    """Entry point for the bootstrap tool."""
    parser = argparse.ArgumentParser(description="Generate an eval case skeleton from a PDF.")
    parser.add_argument("pdf", type=Path, help="Path to the PDF to bootstrap a case from")
    parser.add_argument("--out", type=Path, default=None, help="Output path (defaults to evals/cases/<stem>.json)")
    args = parser.parse_args()

    pdf_path = args.pdf.resolve()
    skeleton = build_skeleton(pdf_path)
    out_path = args.out or CASES_DIR / f"{pdf_path.stem}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(skeleton, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"Wrote {out_path} with {len(skeleton['expected_labels'])} candidate labels.")  # ty: ignore[invalid-argument-type]
    print("Curate expected_labels before using this case.")


if __name__ == "__main__":
    main()

"""Table shaping, page filtering, export and validation for the review UI."""

import io
from typing import Any

import pandas as pd

CONTEXT_SEPARATOR = " › "
TABLE_COLUMNS = ("display_name", "name", "label", "context", "page")
TEXT_COLUMNS = ("display_name", "name", "label", "context")

# Comma-separated with a BOM: the BOM keeps umlauts intact when the file is opened in Excel,
# while the comma keeps the file a standard CSV for machine ingest.
CSV_ENCODING = "utf-8-sig"
CSV_SEPARATOR = ","


def to_dataframe(fields: list[dict[str, Any]]) -> pd.DataFrame:
    """Turn API fields into the table shown in the editor.

    Args:
        fields: The `fields` array of an extraction result.

    Returns:
        A dataframe with one row per field and the editor's column layout.
    """
    rows = [
        {
            "display_name": field.get("display_name", ""),
            "name": field["name"],
            "label": field["label"],
            "context": CONTEXT_SEPARATOR.join(field["context_path"]),
            "page": field["page"],
        }
        for field in fields
    ]
    return pd.DataFrame(rows, columns=list(TABLE_COLUMNS))


def page_numbers(frame: pd.DataFrame, page_count: int) -> list[int]:
    """List the pages to offer in the page selector.

    Every page of the document is offered, including pages where nothing was found, so the
    reviewer can see for themselves that a page really is empty.

    Args:
        frame: The field table.
        page_count: Number of pages in the document.

    Returns:
        Page numbers in ascending order, starting at 1.
    """
    pages = {int(page) for page in frame["page"].dropna()} if not frame.empty else set()
    pages.update(range(1, max(page_count, 0) + 1))
    return sorted(pages)


def filter_page(frame: pd.DataFrame, page: int) -> pd.DataFrame:
    """Return only the rows belonging to one page.

    Args:
        frame: The field table.
        page: The 1-based page number.

    Returns:
        The rows for that page, in their existing order.
    """
    if frame.empty:
        return frame
    return frame[frame["page"] == page]


def merge_page_edits(frame: pd.DataFrame, edited: pd.DataFrame, page: int) -> pd.DataFrame:
    """Fold edits made on one page back into the full table.

    The editor only ever shows one page, so rows added there belong to that page and rows
    missing from it were deleted. Everything outside the page is carried over untouched.

    Args:
        frame: The full field table before editing.
        edited: What the editor returned for `page`.
        page: The page that was being edited.

    Returns:
        The full table with the edits applied, ordered by page.
    """
    others = frame[frame["page"] != page] if not frame.empty else frame

    rows = edited.copy()
    rows["page"] = page
    rows = normalise_table(rows, default_page=page)

    merged = pd.concat([others, rows], ignore_index=True)
    return merged.sort_values("page", kind="stable").reset_index(drop=True)


def normalise_table(frame: pd.DataFrame, *, default_page: int = 1) -> pd.DataFrame:
    """Repair a table after free-form editing.

    The editor lets rows be added with empty cells, so blanks are filled and the page is
    coerced back to a whole number before the table is stored or exported.

    Args:
        frame: The table as the editor returned it.
        default_page: Page to assign to rows that have none.

    Returns:
        A table with the expected columns, no missing values, and an integer page.
    """
    repaired = frame.copy()

    for column in TEXT_COLUMNS:
        if column not in repaired.columns:
            repaired[column] = ""
        repaired[column] = repaired[column].fillna("").astype(str)

    if "page" not in repaired.columns:
        repaired["page"] = default_page
    pages = pd.to_numeric(repaired["page"], errors="coerce").fillna(default_page)
    repaired["page"] = pages.astype(int)

    return repaired[list(TABLE_COLUMNS)].reset_index(drop=True)


def to_csv_bytes(frame: pd.DataFrame) -> bytes:
    """Serialise the field inventory for download.

    Args:
        frame: The edited field table.

    Returns:
        The table as CSV, ready to hand to a download button.
    """
    buffer = io.StringIO()
    normalise_table(frame).to_csv(buffer, index=False, sep=CSV_SEPARATOR)
    return buffer.getvalue().encode(CSV_ENCODING)


def find_duplicates(names: "pd.Series[Any]") -> list[str]:
    """Return the field names that occur more than once, ignoring blanks.

    Checked across the whole document, not just the page on screen, because that is where
    uniqueness actually has to hold.

    Args:
        names: The `name` column of the edited table.

    Returns:
        The duplicated names, sorted.
    """
    if names.empty:
        return []

    cleaned = names.fillna("").astype(str).str.strip()
    non_empty = cleaned[cleaned != ""]
    if non_empty.empty:
        return []

    return sorted(non_empty[non_empty.duplicated(keep=False)].unique().tolist())

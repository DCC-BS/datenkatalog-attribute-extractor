"""Table shaping and validation for the review UI."""

from typing import Any

import pandas as pd

CONTEXT_SEPARATOR = " › "
TABLE_COLUMNS = ("name", "label", "context", "page")


def to_dataframe(fields: list[dict[str, Any]]) -> pd.DataFrame:
    """Turn API fields into the table shown in the editor.

    Args:
        fields: The `fields` array of an extraction result.

    Returns:
        A dataframe with one row per field and the editor's column layout.
    """
    rows = [
        {
            "name": field["name"],
            "label": field["label"],
            "context": CONTEXT_SEPARATOR.join(field["context_path"]),
            "page": field["page"],
        }
        for field in fields
    ]
    return pd.DataFrame(rows, columns=list(TABLE_COLUMNS))


def find_duplicates(names: "pd.Series[Any]") -> list[str]:
    """Return the field names that occur more than once, ignoring blanks.

    Run after every user edit, so the reviewer cannot hand on a colliding inventory.

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

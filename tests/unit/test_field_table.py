"""Tests for the review table's shaping, page filtering and duplicate validation."""

import pandas as pd

from datenkatalog_attribute_extractor.ui.field_table import (
    TABLE_COLUMNS,
    filter_page,
    find_duplicates,
    merge_page_edits,
    normalise_table,
    page_numbers,
    to_csv_bytes,
    to_dataframe,
)


def make_frame(rows: list[tuple[str, str, str, str, int]]) -> pd.DataFrame:
    """Build a field table from (display_name, name, label, context, page) tuples."""
    return pd.DataFrame(rows, columns=list(TABLE_COLUMNS))


SAMPLE = make_frame(
    [
        ("Vater Name", "vater_name", "Name", "Vertreter › Vater", 1),
        ("Mutter Name", "mutter_name", "Name", "Vertreter › Mutter", 1),
        ("Schule", "schule", "Schule", "Aktuelle Schule", 2),
        ("Unterschrift", "unterschrift", "Unterschrift", "", 3),
    ]
)


def test_to_dataframe_flattens_the_context_path() -> None:
    frame = to_dataframe(
        [
            {
                "name": "vater_familienname",
                "display_name": "Vater Familienname",
                "label": "Familienname:",
                "context_path": ["Vertreter", "Vater"],
                "page": 1,
            },
        ]
    )

    assert list(frame.columns) == list(TABLE_COLUMNS)
    assert frame.loc[0, "context"] == "Vertreter › Vater"
    assert frame.loc[0, "name"] == "vater_familienname"
    assert frame.loc[0, "display_name"] == "Vater Familienname"


def test_to_dataframe_tolerates_a_response_without_display_names() -> None:
    """An older API build must not break the table."""
    frame = to_dataframe([{"name": "name", "label": "Name:", "context_path": [], "page": 1}])

    assert frame.loc[0, "display_name"] == ""


def test_to_dataframe_with_no_fields_returns_an_empty_table_with_columns() -> None:
    frame = to_dataframe([])

    assert frame.empty
    assert list(frame.columns) == list(TABLE_COLUMNS)


def test_page_numbers_offers_every_page_of_the_document() -> None:
    """Pages where nothing was found must still be selectable, so they can be checked."""
    assert page_numbers(SAMPLE, page_count=5) == [1, 2, 3, 4, 5]


def test_page_numbers_with_an_empty_table_still_offers_the_document_pages() -> None:
    assert page_numbers(to_dataframe([]), page_count=3) == [1, 2, 3]


def test_filter_page_returns_only_that_page() -> None:
    rows = filter_page(SAMPLE, 1)

    assert list(rows["name"]) == ["vater_name", "mutter_name"]


def test_filter_page_for_a_page_without_fields_returns_nothing() -> None:
    assert filter_page(SAMPLE, 4).empty


def test_merge_page_edits_applies_a_rename_without_touching_other_pages() -> None:
    edited = filter_page(SAMPLE, 1).copy()
    edited.loc[edited.index[0], "name"] = "vater_nachname"

    merged = merge_page_edits(SAMPLE, edited, 1)

    assert set(merged["name"]) == {"vater_nachname", "mutter_name", "schule", "unterschrift"}
    assert len(merged) == 4


def test_merge_page_edits_keeps_rows_ordered_by_page() -> None:
    merged = merge_page_edits(SAMPLE, filter_page(SAMPLE, 2), 2)

    assert list(merged["page"]) == [1, 1, 2, 3]


def test_merge_page_edits_assigns_the_current_page_to_a_new_row() -> None:
    edited = pd.concat(
        [filter_page(SAMPLE, 2), pd.DataFrame([{"name": "nachgetragen", "label": "", "context": ""}])],
        ignore_index=True,
    )

    merged = merge_page_edits(SAMPLE, edited, 2)

    added = merged[merged["name"] == "nachgetragen"]
    assert len(added) == 1
    assert added.iloc[0]["page"] == 2


def test_merge_page_edits_drops_a_row_deleted_on_that_page() -> None:
    edited = filter_page(SAMPLE, 1).iloc[1:]

    merged = merge_page_edits(SAMPLE, edited, 1)

    assert "vater_name" not in set(merged["name"])
    assert len(merged) == 3


def test_merge_page_edits_on_a_page_with_no_fields_adds_to_that_page() -> None:
    edited = pd.DataFrame([{"name": "neu", "label": "", "context": ""}])

    merged = merge_page_edits(SAMPLE, edited, 4)

    assert merged[merged["name"] == "neu"].iloc[0]["page"] == 4
    assert len(merged) == 5


def test_merge_page_edits_is_a_no_op_when_nothing_changed() -> None:
    merged = merge_page_edits(SAMPLE, filter_page(SAMPLE, 1), 1)

    assert list(merged["name"]) == list(SAMPLE["name"])
    assert list(merged["page"]) == list(SAMPLE["page"])


def test_normalise_table_fills_blank_cells_left_by_the_editor() -> None:
    frame = pd.DataFrame([{"display_name": "Neu", "name": "neu", "page": 2}])

    repaired = normalise_table(frame)

    assert list(repaired.columns) == list(TABLE_COLUMNS)
    assert repaired.loc[0, "label"] == ""
    assert repaired.loc[0, "context"] == ""


def test_normalise_table_coerces_the_page_back_to_a_whole_number() -> None:
    frame = pd.DataFrame([{"display_name": "A", "name": "a", "label": "", "context": "", "page": "3"}])

    assert normalise_table(frame).loc[0, "page"] == 3


def test_normalise_table_assigns_a_default_page_to_a_row_without_one() -> None:
    frame = pd.DataFrame([{"display_name": "A", "name": "a", "label": "", "context": "", "page": None}])

    assert normalise_table(frame, default_page=4).loc[0, "page"] == 4


def test_to_csv_bytes_writes_a_header_and_one_line_per_field() -> None:
    text = to_csv_bytes(SAMPLE).decode("utf-8-sig")
    lines = text.strip().splitlines()

    assert lines[0] == "display_name,name,label,context,page"
    assert len(lines) == len(SAMPLE) + 1


def test_to_csv_bytes_starts_with_a_bom_so_excel_reads_umlauts() -> None:
    frame = make_frame([("Grösse", "groesse", "Grösse", "Angaben", 1)])

    payload = to_csv_bytes(frame)

    assert payload.startswith(b"\xef\xbb\xbf")
    assert "Grösse" in payload.decode("utf-8-sig")


def test_to_csv_bytes_quotes_labels_that_contain_the_separator() -> None:
    frame = make_frame([("Ort, Datum", "ort_datum", "Ort, Datum", "", 5)])

    text = to_csv_bytes(frame).decode("utf-8-sig")

    assert '"Ort, Datum"' in text
    assert text.strip().splitlines()[1].count(",") > 1


def test_to_csv_bytes_exports_the_edited_state_not_the_original() -> None:
    edited = SAMPLE.copy()
    edited.loc[0, "name"] = "vater_nachname"

    text = to_csv_bytes(edited).decode("utf-8-sig")

    assert "vater_nachname" in text
    assert "vater_name" not in text


def test_to_csv_bytes_with_an_empty_table_still_writes_the_header() -> None:
    text = to_csv_bytes(to_dataframe([])).decode("utf-8-sig")

    assert text.strip() == "display_name,name,label,context,page"


def test_find_duplicates_with_unique_names_returns_nothing() -> None:
    assert find_duplicates(pd.Series(["name", "vorname", "ort"])) == []


def test_find_duplicates_reports_every_repeated_name_once() -> None:
    assert find_duplicates(pd.Series(["name", "name", "ort", "ort", "plz"])) == ["name", "ort"]


def test_find_duplicates_ignores_blank_and_missing_names() -> None:
    assert find_duplicates(pd.Series(["", "  ", None, "name"])) == []


def test_find_duplicates_treats_surrounding_whitespace_as_equal() -> None:
    assert find_duplicates(pd.Series(["name", " name "])) == ["name"]


def test_find_duplicates_with_an_empty_column_returns_nothing() -> None:
    assert find_duplicates(pd.Series([], dtype=object)) == []


def test_a_rename_that_collides_across_pages_is_detected() -> None:
    """Uniqueness has to hold document-wide, not just on the page being edited."""
    edited = filter_page(SAMPLE, 1).copy()
    edited.loc[edited.index[0], "name"] = "schule"

    merged = merge_page_edits(SAMPLE, edited, 1)

    assert find_duplicates(merged["name"]) == ["schule"]

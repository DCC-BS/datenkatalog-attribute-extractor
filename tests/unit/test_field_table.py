"""Tests for the review table's shaping and duplicate validation."""

import pandas as pd

from datenkatalog_attribute_extractor.ui.field_table import TABLE_COLUMNS, find_duplicates, to_dataframe


def test_to_dataframe_flattens_the_context_path() -> None:
    frame = to_dataframe(
        [
            {"name": "vater_familienname", "label": "Familienname:", "context_path": ["Vertreter", "Vater"], "page": 1},
        ]
    )

    assert list(frame.columns) == list(TABLE_COLUMNS)
    assert frame.loc[0, "context"] == "Vertreter › Vater"
    assert frame.loc[0, "name"] == "vater_familienname"


def test_to_dataframe_with_no_fields_returns_an_empty_table_with_columns() -> None:
    frame = to_dataframe([])

    assert frame.empty
    assert list(frame.columns) == list(TABLE_COLUMNS)


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

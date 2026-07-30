"""Tests for unique name assignment — the core guarantee of the service."""

import pytest

from datenkatalog_attribute_extractor.services.naming import (
    MAX_NAME_LENGTH,
    ensure_unique_names,
    slugify,
)
from tests.factories import make_field


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Name:", "name"),
        ("Geburtsdatum/Ort:", "geburtsdatum_ort"),
        ("*Strasse:", "strasse"),
        ("AHV-Nummer:", "ahv_nummer"),
        ("Vorname(n):", "vorname_n"),
        ("Erziehungs- und Korrespondenzberechtigt", "erziehungs_und_korrespondenzberechtigt"),
        ("  Telefon  ", "telefon"),
    ],
)
def test_slugify_normalises_form_labels(label: str, expected: str) -> None:
    assert slugify(label) == expected


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Grösse", "groesse"),
        ("Strasse", "strasse"),
        ("Straße", "strasse"),
        ("Müller", "mueller"),
        ("Zürich", "zuerich"),
        ("Ärztin", "aerztin"),
        ("Prénom", "prenom"),
    ],
)
def test_slugify_transliterates_german_characters_instead_of_dropping_them(label: str, expected: str) -> None:
    assert slugify(label) == expected


def test_slugify_with_only_punctuation_returns_empty_string() -> None:
    assert slugify("*** :") == ""


def test_ensure_unique_names_with_no_fields_returns_empty_result() -> None:
    fields, warnings = ensure_unique_names([])

    assert fields == []
    assert warnings == []


def test_ensure_unique_names_with_distinct_labels_keeps_plain_slugs() -> None:
    fields, warnings = ensure_unique_names(
        [
            make_field(label="Name:", context_path=["Angaben Schüler"]),
            make_field(label="Vorname:", context_path=["Angaben Schüler"]),
        ]
    )

    assert [field.name for field in fields] == ["name", "vorname"]
    assert warnings == []


def test_ensure_unique_names_disambiguates_parent_columns_by_column_header() -> None:
    fields, _ = ensure_unique_names(
        [
            make_field(label="Familienname:", context_path=["Gesetzliche Vertreter", "Vater"]),
            make_field(label="Familienname:", context_path=["Gesetzliche Vertreter", "Mutter"]),
        ]
    )

    assert [field.name for field in fields] == ["vater_familienname", "mutter_familienname"]


def test_ensure_unique_names_never_leaves_a_bare_name_beside_a_prefixed_sibling() -> None:
    fields, _ = ensure_unique_names(
        [
            make_field(label="E-Mail:", context_path=["Vater"]),
            make_field(label="E-Mail:", context_path=["Mutter"]),
        ]
    )

    names = {field.name for field in fields}
    assert "e_mail" not in names
    assert names == {"vater_e_mail", "mutter_e_mail"}


def test_ensure_unique_names_resolves_a_three_way_collision_across_pages() -> None:
    fields, warnings = ensure_unique_names(
        [
            make_field(label="E-Mail:", context_path=["Gesetzliche Vertreter", "Vater"], page=1),
            make_field(label="E-Mail:", context_path=["Gesetzliche Vertreter", "Mutter"], page=1),
            make_field(label="E-Mail:", context_path=["Aktuelle Schule"], page=2),
        ]
    )

    assert [field.name for field in fields] == ["vater_e_mail", "mutter_e_mail", "aktuelle_schule_e_mail"]
    assert len(warnings) == 3


def test_ensure_unique_names_uses_deeper_context_when_the_innermost_level_is_ambiguous() -> None:
    fields, _ = ensure_unique_names(
        [
            make_field(label="Strasse:", context_path=["Vater", "Adresse"]),
            make_field(label="Strasse:", context_path=["Mutter", "Adresse"]),
        ]
    )

    assert [field.name for field in fields] == ["vater_adresse_strasse", "mutter_adresse_strasse"]


def test_ensure_unique_names_falls_back_to_page_number_without_usable_context() -> None:
    fields, _ = ensure_unique_names(
        [
            make_field(label="Unterschrift:", context_path=[], page=3),
            make_field(label="Unterschrift:", context_path=[], page=7),
        ]
    )

    assert [field.name for field in fields] == ["unterschrift_p3", "unterschrift_p7"]


def test_ensure_unique_names_falls_back_to_a_numeric_suffix_when_nothing_distinguishes_fields() -> None:
    fields, _ = ensure_unique_names(
        [
            make_field(label="Bemerkung:", context_path=[], page=1),
            make_field(label="Bemerkung:", context_path=[], page=1),
            make_field(label="Bemerkung:", context_path=[], page=1),
        ]
    )

    names = [field.name for field in fields]
    assert len(set(names)) == 3
    assert names[0] == "bemerkung"
    assert names[1:] == ["bemerkung_2", "bemerkung_3"]


def test_ensure_unique_names_avoids_colliding_with_an_already_unique_name() -> None:
    fields, _ = ensure_unique_names(
        [
            make_field(label="Vater Familienname", context_path=[]),
            make_field(label="Familienname:", context_path=["Vater"]),
            make_field(label="Familienname:", context_path=["Mutter"]),
        ]
    )

    names = [field.name for field in fields]
    assert names[0] == "vater_familienname"
    assert len(set(names)) == 3


def test_ensure_unique_names_with_unlabelled_field_uses_a_fallback_name() -> None:
    fields, _ = ensure_unique_names([make_field(label="***", context_path=[])])

    assert fields[0].name == "feld"


def test_ensure_unique_names_respects_the_length_cap() -> None:
    long_label = "Sehr ausfuehrliche Beschreibung eines Feldes das viel zu lang benannt ist und weiter geht"
    fields, _ = ensure_unique_names(
        [
            make_field(label=long_label, context_path=["Vater"]),
            make_field(label=long_label, context_path=["Mutter"]),
        ]
    )

    names = [field.name for field in fields]
    assert all(len(name) <= MAX_NAME_LENGTH for name in names)
    assert names[0].startswith("vater_")
    assert names[1].startswith("mutter_")
    assert len(set(names)) == 2


def test_ensure_unique_names_preserves_input_order_and_metadata() -> None:
    source = [
        make_field(label="Name:", context_path=["A"], page=1),
        make_field(label="Ort:", context_path=["B"], page=2),
    ]

    fields, _ = ensure_unique_names(source)

    assert [field.label for field in fields] == ["Name:", "Ort:"]
    assert [field.page for field in fields] == [1, 2]
    assert [field.context_path for field in fields] == [["A"], ["B"]]


def test_ensure_unique_names_always_produces_globally_unique_names() -> None:
    labels = ["Name:", "Vorname:", "E-Mail:", "Telefon:"]
    contexts = [["Vater"], ["Mutter"], [], ["Schule"]]
    source = [
        make_field(label=label, context_path=context, page=page)
        for page in (1, 2)
        for label in labels
        for context in contexts
    ]

    fields, _ = ensure_unique_names(source)

    assert len({field.name for field in fields}) == len(source)

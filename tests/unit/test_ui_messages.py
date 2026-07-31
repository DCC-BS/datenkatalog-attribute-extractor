"""Tests for the reviewer-facing error messages."""

from datenkatalog_attribute_extractor.models.field import ExtractedField
from datenkatalog_attribute_extractor.services.naming import ensure_unique_names
from datenkatalog_attribute_extractor.ui.messages import describe_error, split_warnings


def test_describe_error_blames_the_model_service_not_the_upload_on_503() -> None:
    message = describe_error(
        {
            "errorId": "service_unavailable",
            "status": 503,
            "debugMessage": "The LLM API at http://llm:8000/health is unavailable: Connection refused",
        }
    )

    assert "Sprachmodell ist nicht erreichbar" in message
    assert "nicht ausgewertet" in message


def test_describe_error_explains_an_unsupported_file_type() -> None:
    message = describe_error({"status": 415, "debugMessage": "No extractor registered for 'text/plain'"})

    assert "Dateityp" in message
    assert "PDF" in message


def test_describe_error_falls_back_to_the_debug_message() -> None:
    message = describe_error({"status": 400, "debugMessage": "The uploaded document is empty"})

    assert "The uploaded document is empty" in message


def test_describe_error_handles_a_payload_without_a_message() -> None:
    assert "unbekannter Fehler" in describe_error({})


def test_split_warnings_keeps_a_real_problem_out_of_the_rename_noise() -> None:
    warnings = [
        "Duplicate label 'E-Mail:' on page 1 disambiguated to 'vater_e_mail'",
        "Page 5 could not be processed: boom",
        "Duplicate label 'E-Mail:' on page 1 disambiguated to 'mutter_e_mail'",
    ]

    renames, problems = split_warnings(warnings)

    assert len(renames) == 2
    assert problems == ["Page 5 could not be processed: boom"]


def test_split_warnings_with_no_warnings_returns_two_empty_lists() -> None:
    assert split_warnings([]) == ([], [])


def test_split_warnings_matches_the_messages_the_naming_pass_actually_emits() -> None:
    """Guards the prefix coupling: a reworded warning must not silently become a 'problem'."""
    fields = [
        ExtractedField(label="E-Mail:", context_path=["Vater"], page=1),
        ExtractedField(label="E-Mail:", context_path=["Mutter"], page=1),
    ]
    _, warnings = ensure_unique_names(fields)

    renames, problems = split_warnings(warnings)

    assert len(renames) == len(warnings) == 2
    assert problems == []

"""Tests for the reviewer-facing error messages."""

from datenkatalog_attribute_extractor.ui.messages import describe_error


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

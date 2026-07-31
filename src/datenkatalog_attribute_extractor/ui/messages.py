"""User-facing message text for the review UI."""

from typing import Any

from datenkatalog_attribute_extractor.services.naming import DUPLICATE_WARNING_PREFIX

HTTP_SERVICE_UNAVAILABLE = 503
HTTP_UNSUPPORTED_MEDIA_TYPE = 415


def split_warnings(warnings: list[str]) -> tuple[list[str], list[str]]:
    """Separate routine renames from warnings that need attention.

    A form with repeated captions produces dozens of rename notices. Listing them next to a
    genuine problem — a page that could not be read — buries the one entry that matters.

    Args:
        warnings: The `warnings` array of an extraction result.

    Returns:
        A tuple of (renames, problems).
    """
    renames = [warning for warning in warnings if warning.startswith(DUPLICATE_WARNING_PREFIX)]
    problems = [warning for warning in warnings if not warning.startswith(DUPLICATE_WARNING_PREFIX)]
    return renames, problems


def describe_error(payload: dict[str, Any]) -> str:
    """Turn an API error payload into a message that points at the actual cause.

    An unavailable model is an infrastructure problem, not a problem with the uploaded file,
    and the reviewer should not be left guessing which of the two happened.

    Args:
        payload: The decoded `error` event or error response body.

    Returns:
        A German message for the reviewer.
    """
    detail = payload.get("debugMessage") or "unbekannter Fehler"
    status = payload.get("status")

    if status == HTTP_SERVICE_UNAVAILABLE:
        return (
            "Das Sprachmodell ist nicht erreichbar. Das Formular wurde nicht ausgewertet – "
            f"bitte den LLM-Dienst prüfen und erneut versuchen. ({detail})"
        )
    if status == HTTP_UNSUPPORTED_MEDIA_TYPE:
        return f"Dieser Dateityp wird nicht unterstützt. Bitte ein PDF hochladen. ({detail})"
    return f"Die Extraktion ist fehlgeschlagen: {detail}"

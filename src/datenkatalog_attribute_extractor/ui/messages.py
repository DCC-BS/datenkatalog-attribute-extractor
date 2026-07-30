"""User-facing message text for the review UI."""

from typing import Any

HTTP_SERVICE_UNAVAILABLE = 503
HTTP_UNSUPPORTED_MEDIA_TYPE = 415


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

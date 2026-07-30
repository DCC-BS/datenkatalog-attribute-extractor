"""Streamlit review UI for extracted form fields.

A thin HTTP client over the extraction API, so the API stays independently usable. Results
live in the session only: there is no persistence and no export, because the downstream
consumer cannot ingest files yet.
"""

import os
from typing import Any

import httpx
import streamlit as st

from datenkatalog_attribute_extractor.ui.field_table import find_duplicates, to_dataframe
from datenkatalog_attribute_extractor.ui.messages import describe_error
from datenkatalog_attribute_extractor.utils.sse import parse_sse

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")
STREAM_ENDPOINT = "/extraction/form-fields/stream"
REQUEST_TIMEOUT = httpx.Timeout(None, connect=10.0)

RESULT_KEY = "extraction_result"
SOURCE_KEY = "extraction_source"


def run_extraction(filename: str, content: bytes, media_type: str) -> dict[str, Any] | None:
    """Send a document to the API and report progress while it is processed.

    Extraction can take minutes because pages are read one at a time, so progress is driven
    by the API's server-sent events rather than a plain spinner.

    Args:
        filename: Name of the uploaded file.
        content: Raw file bytes.
        media_type: The upload's media type.

    Returns:
        The extraction result, or `None` if the run failed.
    """
    progress = st.progress(0.0, text="Dokument wird gesendet …")
    result: dict[str, Any] | None = None

    try:
        with (
            httpx.Client(timeout=REQUEST_TIMEOUT) as client,
            client.stream(
                "POST",
                f"{BACKEND_URL}{STREAM_ENDPOINT}",
                files={"file": (filename, content, media_type)},
            ) as response,
        ):
            if response.status_code != httpx.codes.OK:
                response.read()
                try:
                    payload = response.json()
                except ValueError:
                    payload = {"status": response.status_code, "debugMessage": response.text}
                st.error(describe_error(payload))
                return None

            for event, payload in parse_sse(response.iter_lines()):
                if event == "progress":
                    total = max(payload["total_pages"], 1)
                    done = payload["page"]
                    progress.progress(
                        min(done / total, 1.0),
                        text=f"Seite {done} von {total} – {payload['fields_found']} Felder gefunden",
                    )
                elif event == "result":
                    result = payload
                elif event == "error":
                    st.error(describe_error(payload))
                    return None
    except httpx.HTTPError as error:
        st.error(f"Die API ist nicht erreichbar ({BACKEND_URL}): {error}")
        return None
    finally:
        progress.empty()

    return result


def render_results(result: dict[str, Any]) -> None:
    """Render the editable field table and its validation state.

    Args:
        result: The extraction result returned by the API.
    """
    fields = result["fields"]
    st.subheader(f"{len(fields)} Felder aus {result['page_count']} Seiten")

    edited = st.data_editor(
        to_dataframe(fields),
        width="stretch",
        hide_index=True,
        num_rows="dynamic",
        column_config={
            "name": st.column_config.TextColumn(
                "Feldname",
                help="Eindeutiger Name für den Datenkatalog. Bearbeitbar.",
                required=True,
            ),
            "label": st.column_config.TextColumn("Beschriftung im Formular", disabled=True),
            "context": st.column_config.TextColumn("Abschnitt", disabled=True),
            "page": st.column_config.NumberColumn("Seite", disabled=True),
        },
        key="field_editor",
    )

    duplicates = find_duplicates(edited["name"])
    if duplicates:
        st.error("Diese Feldnamen sind mehrfach vergeben: " + ", ".join(f"`{name}`" for name in duplicates))
    else:
        st.success("Alle Feldnamen sind eindeutig.")

    warnings = result.get("warnings", [])
    if warnings:
        with st.expander(f"{len(warnings)} Hinweise zur Extraktion"):
            for warning in warnings:
                st.write(f"- {warning}")


def main() -> None:
    """Run the Streamlit application."""
    st.set_page_config(page_title="Formularfeld-Extraktion", page_icon="📄", layout="wide")
    st.title("Formularfeld-Extraktion")
    st.caption(
        "PDF-Formular hochladen, Felder automatisch erkennen lassen und die vorgeschlagenen "
        "Feldnamen prüfen und korrigieren."
    )

    upload = st.file_uploader("PDF-Formular", type=["pdf"], accept_multiple_files=False)

    if upload is not None and st.button("Felder extrahieren", type="primary"):
        result = run_extraction(upload.name, upload.getvalue(), upload.type or "application/pdf")
        if result is not None:
            st.session_state[RESULT_KEY] = result
            st.session_state[SOURCE_KEY] = upload.name

    result = st.session_state.get(RESULT_KEY)
    if result is None:
        st.info("Noch kein Formular ausgewertet.")
        return

    st.divider()
    st.caption(f"Quelle: {st.session_state.get(SOURCE_KEY, 'unbekannt')}")
    render_results(result)


main()

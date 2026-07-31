"""Streamlit review UI for extracted form fields.

A thin HTTP client over the extraction API, so the API stays independently usable. Results
live in the session only — nothing is stored server-side — but the reviewed table can be
downloaded as CSV.

The layout puts input and status in the sidebar and gives the whole main area to the work.
Two views share the same editable table:

* *Vergleich mit PDF* — the rendered page beside the fields found on that page.
* *Alle Felder* — the whole document in one table, with the CSV export.
"""

import os
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
import streamlit as st

from datenkatalog_attribute_extractor.ui.field_table import (
    filter_page,
    find_duplicates,
    merge_page_edits,
    normalise_table,
    page_numbers,
    to_csv_bytes,
    to_dataframe,
)
from datenkatalog_attribute_extractor.ui.messages import describe_error, split_warnings
from datenkatalog_attribute_extractor.ui.pdf_preview import render_previews
from datenkatalog_attribute_extractor.utils.sse import parse_sse

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")
STREAM_ENDPOINT = "/extraction/form-fields/stream"
REQUEST_TIMEOUT = httpx.Timeout(None, connect=10.0)

RESULT_KEY = "extraction_result"
FIELDS_KEY = "extraction_fields"
PDF_KEY = "extraction_pdf"
SOURCE_KEY = "extraction_source"

VIEW_COMPARISON = "Vergleich mit PDF"
VIEW_ALL_FIELDS = "Alle Felder"

NAME_COLUMN = st.column_config.TextColumn(
    "Feldname",
    help="Lesbarer Name, so geschrieben wie im Formular. Bearbeitbar.",
    required=True,
    width="medium",
)
TECH_NAME_COLUMN = st.column_config.TextColumn(
    "Tech Feldname",
    help="Technischer, eindeutiger Name für den Datenkatalog. Bearbeitbar.",
    required=True,
    width="medium",
)

# Beyond this many pages a row of buttons stops fitting, so fall back to a compact selector.
MAX_SEGMENTED_PAGES = 12

# Roughly the height of an A4 page rendered into half the main area, so the table ends level
# with the page image instead of leaving a band of empty space beside it.
TABLE_HEIGHT_PX = 820


@st.cache_data(show_spinner=False, max_entries=4)
def cached_previews(pdf_bytes: bytes) -> list[bytes]:
    """Render and cache page previews, so switching pages does not re-rasterise the PDF."""
    return render_previews(pdf_bytes)


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
                        text=f"Seite {done} von {total} – {payload['fields_found']} Felder",
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


def render_sidebar() -> None:
    """Draw the upload controls and the run summary."""
    st.sidebar.markdown("#### Formularfeld-Extraktion")

    upload = st.sidebar.file_uploader("PDF-Formular", type=["pdf"], accept_multiple_files=False)
    start = st.sidebar.button(
        "Felder extrahieren",
        type="primary",
        width="stretch",
        disabled=upload is None,
    )

    if upload is not None and start:
        content = upload.getvalue()
        with st.sidebar:
            result = run_extraction(upload.name, content, upload.type or "application/pdf")
        if result is not None:
            st.session_state[RESULT_KEY] = result
            st.session_state[FIELDS_KEY] = to_dataframe(result["fields"])
            st.session_state[PDF_KEY] = content
            st.session_state[SOURCE_KEY] = upload.name

    result = st.session_state.get(RESULT_KEY)
    if result is None:
        return

    frame: pd.DataFrame = st.session_state[FIELDS_KEY]
    duplicates = find_duplicates(frame["name"])

    st.sidebar.divider()
    st.sidebar.caption(st.session_state.get(SOURCE_KEY, "unbekannt"))

    left, right = st.sidebar.columns(2)
    left.metric("Felder", len(frame))
    right.metric("Seiten", result["page_count"])

    if duplicates:
        st.sidebar.error(f"{len(duplicates)} Feldnamen doppelt: " + ", ".join(f"`{name}`" for name in duplicates))
    else:
        st.sidebar.success("Alle Feldnamen sind eindeutig.")

    renames, problems = split_warnings(result.get("warnings", []))

    for problem in problems:
        st.sidebar.warning(problem)

    if renames:
        with st.sidebar.expander(f"{len(renames)} automatisch umbenannt"):
            st.caption(
                "Diese Beschriftungen kommen im Formular mehrfach vor und wurden anhand ihres "
                "Abschnitts eindeutig gemacht."
            )
            for rename in renames:
                st.write(f"- {rename}")


def select_page(pages: list[int]) -> int:
    """Draw the page selector and return the chosen page."""
    if len(pages) <= MAX_SEGMENTED_PAGES:
        selected = st.segmented_control(
            "Seite",
            options=pages,
            default=pages[0],
            format_func=lambda page: str(page),
            label_visibility="collapsed",
            key="page_selector",
        )
        return selected if selected is not None else pages[0]

    return st.selectbox(
        "Seite",
        options=pages,
        format_func=lambda page: f"Seite {page}",
        label_visibility="collapsed",
        key="page_selector",
    )


def render_comparison(result: dict[str, Any]) -> None:
    """Draw the page image next to the fields found on that page."""
    frame: pd.DataFrame = st.session_state[FIELDS_KEY]
    pages = page_numbers(frame, result["page_count"])
    previews = cached_previews(st.session_state[PDF_KEY])

    page = select_page(pages)

    document, fields = st.columns([5, 6], gap="medium")

    with document:
        if 1 <= page <= len(previews):
            st.image(previews[page - 1], width="stretch")
        else:
            st.info("Für diese Seite konnte keine Vorschau erzeugt werden.")

    with fields:
        page_rows = filter_page(frame, page)
        st.caption(f"Seite {page}: {len(page_rows)} von {len(frame)} Feldern")

        # Beschriftung and Abschnitt are omitted here on purpose: the readable name already
        # contains both, and the page itself is on screen next to the table.
        edited = st.data_editor(
            page_rows,
            width="stretch",
            height=TABLE_HEIGHT_PX,
            hide_index=True,
            num_rows="dynamic",
            column_order=("display_name", "name"),
            column_config={
                "display_name": NAME_COLUMN,
                "name": TECH_NAME_COLUMN,
                "label": None,
                "context": None,
                "page": None,
            },
            key=f"field_editor_page_{page}",
        )

        merged = merge_page_edits(frame, edited, page)
        if not merged.equals(frame):
            st.session_state[FIELDS_KEY] = merged
            st.rerun()

        duplicates = find_duplicates(merged["name"])
        if duplicates:
            st.error("Mehrfach vergeben: " + ", ".join(f"`{name}`" for name in duplicates))


def render_all_fields() -> None:
    """Draw every field of the document in one table, with a CSV export."""
    frame: pd.DataFrame = st.session_state[FIELDS_KEY]

    header, download = st.columns([4, 1], vertical_alignment="bottom")
    header.caption(f"{len(frame)} Felder aus dem gesamten Dokument")
    download.download_button(
        "CSV herunterladen",
        data=to_csv_bytes(frame),
        file_name=f"{Path(st.session_state.get(SOURCE_KEY, 'formular')).stem}-felder.csv",
        mime="text/csv",
        width="stretch",
    )

    edited = st.data_editor(
        frame,
        width="stretch",
        height=TABLE_HEIGHT_PX,
        hide_index=True,
        num_rows="dynamic",
        column_config={
            "display_name": NAME_COLUMN,
            "name": TECH_NAME_COLUMN,
            "label": st.column_config.TextColumn("Beschriftung", disabled=True, width="medium"),
            "context": st.column_config.TextColumn("Abschnitt", disabled=True, width="medium"),
            "page": st.column_config.NumberColumn("Seite", min_value=1, step=1, width="small"),
        },
        key="field_editor_all",
    )

    normalised = normalise_table(edited)
    if not normalised.equals(frame):
        st.session_state[FIELDS_KEY] = normalised
        st.rerun()

    duplicates = find_duplicates(normalised["name"])
    if duplicates:
        st.error("Mehrfach vergeben: " + ", ".join(f"`{name}`" for name in duplicates))


def main() -> None:
    """Run the Streamlit application."""
    st.set_page_config(
        page_title="Formularfeld-Extraktion",
        page_icon="📄",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    render_sidebar()

    result = st.session_state.get(RESULT_KEY)
    if result is None:
        st.info("PDF-Formular in der Seitenleiste hochladen und Extraktion starten.")
        return

    # A selector rather than tabs: Streamlit runs the body of every tab on each rerun, which
    # would put two editors on the same table and let them overwrite one another.
    view = st.segmented_control(
        "Ansicht",
        options=[VIEW_COMPARISON, VIEW_ALL_FIELDS],
        default=VIEW_COMPARISON,
        label_visibility="collapsed",
        key="view_selector",
    )

    if view == VIEW_ALL_FIELDS:
        render_all_fields()
    else:
        render_comparison(result)


main()

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
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
URL_STREAM_ENDPOINT = "/extraction/form-fields/url/stream"
REQUEST_TIMEOUT = httpx.Timeout(None, connect=10.0)

RESULT_KEY = "extraction_result"
FIELDS_KEY = "extraction_fields"
PDF_KEY = "extraction_pdf"
SOURCE_KEY = "extraction_source"
SOURCE_KIND_KEY = "extraction_source_kind"

VIEW_COMPARISON = "Vergleich mit Quelle"
VIEW_ALL_FIELDS = "Alle Felder"

INPUT_PDF = "PDF-Formular"
INPUT_URL = "Web-Formular"

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


def run_upload_extraction(filename: str, content: bytes, media_type: str) -> dict[str, Any] | None:
    """Send an uploaded document to the API and report progress.

    Args:
        filename: Name of the uploaded file.
        content: Raw file bytes.
        media_type: The upload's media type.

    Returns:
        The extraction result, or `None` if the run failed.
    """
    return _stream_extraction(
        endpoint=STREAM_ENDPOINT,
        unit="Seite",
        files={"file": (filename, content, media_type)},
    )


def run_url_extraction(url: str) -> dict[str, Any] | None:
    """Send a URL to the API and report progress.

    Args:
        url: Address of the online form.

    Returns:
        The extraction result, or `None` if the run failed.
    """
    # "Teil" rather than "Seite": a web page is split by section to fit the model's context,
    # so the unit here is a part of one page, not a page of a document.
    return _stream_extraction(endpoint=URL_STREAM_ENDPOINT, unit="Teil", json={"url": url})


def _stream_extraction(
    *,
    endpoint: str,
    unit: str,
    files: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Drive one extraction over server-sent events, whatever the source.

    Extraction can take minutes because the work is done one unit at a time, so progress is
    driven by the API's events rather than a plain spinner.

    Args:
        endpoint: The streaming endpoint to call.
        unit: What one step is called in the progress text.
        files: Multipart payload, for uploads.
        json: JSON payload, for URLs.

    Returns:
        The extraction result, or `None` if the run failed.
    """
    progress = st.progress(0.0, text="Anfrage wird gesendet …")
    result: dict[str, Any] | None = None

    try:
        with (
            httpx.Client(timeout=REQUEST_TIMEOUT) as client,
            client.stream("POST", f"{BACKEND_URL}{endpoint}", files=files, json=json) as response,
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
                        text=f"{unit} {done} von {total} – {payload['fields_found']} Felder",
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


def _render_upload_input() -> None:
    """Draw the PDF upload control and run the extraction when asked."""
    upload = st.sidebar.file_uploader("PDF-Formular", type=["pdf"], accept_multiple_files=False)
    start = st.sidebar.button(
        "Felder extrahieren",
        type="primary",
        width="stretch",
        disabled=upload is None,
    )

    if upload is None or not start:
        return

    content = upload.getvalue()
    with st.sidebar:
        result = run_upload_extraction(upload.name, content, upload.type or "application/pdf")
    if result is not None:
        _store_result(result, source_name=upload.name, pdf_bytes=content)


def _render_url_input() -> None:
    """Draw the URL control and run the extraction when asked."""
    url = st.sidebar.text_input(
        "Adresse des Formulars",
        placeholder="https://www.example.ch/anmeldung",
        key="form_url",
    )
    st.sidebar.caption(
        "Es wird nur diese eine Seite gelesen. Mehrstufige Formulare werden erkannt, "
        "die weiteren Schritte aber nicht abgerufen."
    )
    start = st.sidebar.button(
        "Felder extrahieren",
        type="primary",
        width="stretch",
        disabled=not url.strip(),
        key="start_url",
    )

    if not start or not url.strip():
        return

    with st.sidebar:
        result = run_url_extraction(url.strip())
    if result is not None:
        _store_result(result, source_name=url.strip(), pdf_bytes=None)


def _store_result(result: dict[str, Any], *, source_name: str, pdf_bytes: bytes | None) -> None:
    """Put a completed run into the session."""
    st.session_state[RESULT_KEY] = result
    st.session_state[FIELDS_KEY] = to_dataframe(result["fields"])
    st.session_state[PDF_KEY] = pdf_bytes
    st.session_state[SOURCE_KEY] = source_name
    st.session_state[SOURCE_KIND_KEY] = result.get("source_kind", "pdf")


def render_sidebar() -> None:
    """Draw the source controls and the run summary."""
    st.sidebar.markdown("#### Formularfeld-Extraktion")

    source_kind = st.sidebar.radio(
        "Quelle",
        options=[INPUT_PDF, INPUT_URL],
        horizontal=True,
        label_visibility="collapsed",
        key="source_kind_selector",
    )

    if source_kind == INPUT_URL:
        _render_url_input()
    else:
        _render_upload_input()

    result = st.session_state.get(RESULT_KEY)
    if result is None:
        return

    frame: pd.DataFrame = st.session_state[FIELDS_KEY]
    duplicates = find_duplicates(frame["name"])

    st.sidebar.divider()
    st.sidebar.caption(st.session_state.get(SOURCE_KEY, "unbekannt"))

    left, right = st.sidebar.columns(2)
    left.metric("Felder", len(frame))
    right.metric("Teile" if result.get("source_kind") == "web" else "Seiten", result["page_count"])

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


def select_page(pages: list[int], *, unit: str = "Seite") -> int:
    """Draw the page selector and return the chosen page.

    A web form usually fits the model's context in one go, so there is nothing to choose
    between; the selector is skipped rather than shown with a single option.
    """
    if len(pages) <= 1:
        return pages[0] if pages else 1

    if len(pages) <= MAX_SEGMENTED_PAGES:
        selected = st.segmented_control(
            unit,
            options=pages,
            default=pages[0],
            format_func=lambda page: str(page),
            label_visibility="collapsed",
            key="page_selector",
        )
        return selected if selected is not None else pages[0]

    return st.selectbox(
        unit,
        options=pages,
        format_func=lambda page: f"{unit} {page}",
        label_visibility="collapsed",
        key="page_selector",
    )


def _render_web_source() -> None:
    """Show what the model was given for a web form.

    Self-hosted Firecrawl cannot produce screenshots, so there is no picture of the page to
    put here. What is shown instead is arguably more useful for review: a link to the live
    form, and the fields as they were read out of its markup — what the model actually saw,
    rather than what a person would have seen.
    """
    url = st.session_state.get(SOURCE_KEY, "")
    frame: pd.DataFrame = st.session_state[FIELDS_KEY]

    st.link_button("Formular im Browser öffnen", url, width="stretch")
    st.caption(url)
    st.divider()

    st.caption("Struktur, wie sie aus dem HTML gelesen wurde:")
    for context, group in frame.groupby("context", sort=False):
        st.markdown(f"**{context or 'Ohne Abschnitt'}**")
        for label in group["label"]:
            st.markdown(f"- {label}")


def render_comparison(result: dict[str, Any]) -> None:
    """Draw the source next to the fields found in it."""
    frame: pd.DataFrame = st.session_state[FIELDS_KEY]
    pages = page_numbers(frame, result["page_count"])
    is_web = st.session_state.get(SOURCE_KIND_KEY) == "web"

    page = select_page(pages, unit="Teil" if is_web else "Seite")

    document, fields = st.columns([5, 6], gap="medium")

    with document:
        if is_web:
            _render_web_source()
        else:
            previews = cached_previews(st.session_state[PDF_KEY])
            if 1 <= page <= len(previews):
                st.image(previews[page - 1], width="stretch")
            else:
                st.info("Für diese Seite konnte keine Vorschau erzeugt werden.")

    with fields:
        page_rows = filter_page(frame, page)
        unit = "Teil" if is_web else "Seite"
        st.caption(f"{unit} {page}: {len(page_rows)} von {len(frame)} Feldern")

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


def _export_stem() -> str:
    """Build the CSV file name stem from whatever the source was.

    A filename has a usable stem; a URL does not — `Path("https://example.ch/").stem` is
    empty, and a path with query parameters gives something no one wants as a file name. So
    URLs are named after their host and last path segment instead.
    """
    source = st.session_state.get(SOURCE_KEY, "formular")

    if st.session_state.get(SOURCE_KIND_KEY) == "web":
        parts = urlsplit(source)
        segment = Path(parts.path).stem
        host = (parts.hostname or "formular").removeprefix("www.")
        stem = f"{host}-{segment}" if segment else host
        return re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-") or "formular"

    return Path(source).stem or "formular"


def render_all_fields() -> None:
    """Draw every field of the document in one table, with a CSV export."""
    frame: pd.DataFrame = st.session_state[FIELDS_KEY]

    header, download = st.columns([4, 1], vertical_alignment="bottom")
    header.caption(f"{len(frame)} Felder aus dem gesamten Dokument")
    download.download_button(
        "CSV herunterladen",
        data=to_csv_bytes(frame),
        file_name=f"{_export_stem()}-felder.csv",
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
        st.info(
            "In der Seitenleiste ein PDF-Formular hochladen oder die Adresse eines "
            "Web-Formulars eingeben und die Extraktion starten."
        )
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

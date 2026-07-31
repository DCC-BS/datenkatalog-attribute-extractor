# Datenkatalog Attribute Extractor

Extracts the input fields of questionnaires and forms as a list of **uniquely named
attributes** for the Datenkatalog. A PDF is rendered page by page to an image and read by a
locally hosted **Gemma 4** vision model (vLLM, OpenAI-compatible). A reviewer then checks and
corrects the proposed names in a Streamlit UI.

Only the field **label** is extracted — no data types, no answer options, no mandatory flags.
A checkbox or radio group is therefore a single field, named after its group heading.

## How it works

```
PDF ──► pypdfium2 ──► one PNG per page ──► Gemma 4 (vLLM) ──► labels + context
                                                                    │
                                                                    ▼
                                                   ensure_unique_names()  ──► FormField[]
```

Two stages, deliberately separated:

1. **Per-page extraction.** The model reports each label *verbatim* plus its enclosing
   headings (`context_path`), for example `["Gesetzliche Vertreter", "Vater"]`. It never
   invents names.
2. **Deterministic naming.** `services/naming.py` slugifies labels and, only where they
   collide, prefixes them with the innermost distinguishing heading.

Uniqueness is a document-wide property that a page-scoped model call cannot see, and letting
the model choose prefixes would make the core feature untestable. So the model supplies
structure and the code guarantees uniqueness. A form asking twice for `Familienname:` yields
`vater_familienname` and `mutter_familienname`.

Every field carries two names, built from the same label and the same disambiguation:

| | `name` | `display_name` |
|---|---|---|
| Familienname under *Vater* | `vater_familienname` | `Vater Familienname` |
| AHV-Nummer | `ahv_nummer` | `AHV-Nummer` |
| Grösse | `groesse` | `Grösse` |

`display_name` is derived from the label, never by un-slugging `name` — otherwise
`ahv_nummer` would come back as "Ahv Nummer" and `groesse` as "Groesse".

## Reviewing in the UI

Upload and status live in the sidebar; the main area is the work. Two views share one
editable table, so an edit in either is visible in the other:

* **Vergleich mit PDF** — the rendered page next to the fields found on *that* page. One
  selector drives both, which is how you check the extraction against the original.
* **Alle Felder** — the whole document in one table, plus **CSV herunterladen**.

Both name columns are editable. Duplicate detection always runs across the whole document,
not just the visible page. The CSV is comma-separated with a UTF-8 BOM, so umlauts survive
being opened in Excel; switch `CSV_SEPARATOR` in `ui/field_table.py` to `;` if you would
rather have Excel split the columns automatically.

## Quick start

```bash
cp .env.example .env      # IS_PROD is required; init_logger() fails without it
make install
make docker-up            # vLLM + API + UI
```

The UI is on <http://localhost:8501>, the API on <http://localhost:8000/docs>.

Without Docker, against an already-running vLLM:

```bash
make dev      # API on :8000
make dev-ui   # UI on :8501
```

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/extraction/form-fields` | Upload a PDF, get the full inventory. Blocks until done. |
| `POST` | `/extraction/form-fields/stream` | Same, as SSE: a `progress` event per page, then one `result`. |
| `GET` | `/health/{liveness,readiness,startup}` | Kubernetes probes; readiness checks the LLM. |

```bash
curl -F file=@data/example.pdf http://localhost:8000/extraction/form-fields
```

Pages are processed one at a time, so a 7-page form takes minutes. That is why the UI uses
the streaming endpoint. `LLM_MAX_CONCURRENCY` and the vLLM `--max-num-seqs` setting must be
raised together to speed this up.

## Failure behaviour

Extraction is expensive, so failures are split into two kinds.

**Fatal — abort immediately.** Before a single page is rasterised, the service checks that
the LLM answers its health endpoint, serves the configured model, and serves it with a
context large enough for prompt + page image + answer (`LLM_MIN_CONTEXT_TOKENS`, default
8192). The check uses its own short timeout (`LLM_HEALTH_TIMEOUT`, default 5s) rather than
`LLM_TIMEOUT`, so a *hanging* server is caught in seconds. If anything fails — unreachable,
hanging, 5xx, bad credentials, unknown model, context too small — the run stops at once with
**503** and a message naming the endpoint and the cause. Nothing is rendered and no page is
sent. The same applies if the LLM dies mid-run: the next page raises instead of continuing.

```console
$ curl -F file=@data/example.pdf localhost:8000/extraction/form-fields   # LLM down
{"errorId":"service_unavailable","status":503,
 "debugMessage":"The LLM API at http://llm:8000/health is unavailable: ConnectTimeout"}
```

Without this, an unavailable model was the worst case: all pages rendered, every page
failing identically, and an empty inventory returned with one warning per page — a wrong
answer dressed up as a successful run.

**Page-local — carry on.** A page the model cannot read, or a response that will not parse,
yields an empty result plus a warning so the remaining pages still produce output. The
classification lives in `services/llm_health.py::is_fatal_llm_error`.

The app deliberately does **not** refuse to start when the LLM is down; the model may come up
later, and that is what `/health/readiness` is for.

## Development

```bash
make check   # uv lock --locked, ruff format, ruff check, ty
make test    # unit tests
make help    # all targets
```

`make test-integration` runs against a live LLM and is excluded from CI.

## Evaluating extraction quality

Prompt changes silently break form types you are not looking at, so quality is tracked with
labelled cases in `evals/cases/`:

```bash
make eval
```

It reports label precision/recall, the total field count against the expected count, and
fails outright if the naming pass ever produces a duplicate.

Current result on `data/example.pdf` with `gemma-4-31B-it-NVFP4` at 200 DPI and 560 image
tokens: **precision 98.3%, recall 96.6%, 92 of ~93 fields**, all names unique. The two
remaining discrepancies are one very long question the model shortens and one document
checklist on page 5 that it does not report as a field.

To add a case:

```bash
make bootstrap-case PDF=data/your-form.pdf
```

This seeds a skeleton from the PDF's AcroForm widgets, but **the labels must be curated by
hand**. In real questionnaires AcroForm metadata is mostly useless as ground truth: in
`data/example.pdf`, 77 of the 81 text widgets are named `Text1`…`Text31` with no tooltip, and
every checkbox *option* is its own widget even though our specification counts the group as
one field. Treat the widget count as a sanity check, not as an answer key.

## Adding a new source kind

Excel and web pages are planned. Everything after extraction is source-agnostic, so:

1. Implement the `FieldExtractor` protocol in `services/extractors/` — `supports()` plus an
   `extract()` that yields one `PageResult` per unit of work.
2. Add it to `Container.extractor_registry`.

Naming, uniqueness, the API contract and the UI need no changes. Only extractors know about
modality, which matters because Excel and HTML are not naturally images and should not be
forced through the rendering path.

## Structured output: why `NativeOutput`

`services/agents/form_field_agent.py` wraps the output type:

```python
output_type=NativeOutput(PageExtraction)
```

This is load-bearing. A bare `output_type=PageExtraction` makes pydantic-ai request the
result as a **tool call** (`tools: [final_result]`, `tool_choice: "required"`), which vLLM
refuses unless it was started with `--tool-call-parser` and a tool chat template:

```
400 tool_choice="required" requires --tool-call-parser to be set
```

`NativeOutput` instead sends `response_format: json_schema`, which uses vLLM's guided
decoding and needs no extra server flags. That is why the compose command carries no
tool-calling options.


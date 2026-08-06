# Datenkatalog Attribute Extractor

Extracts the input fields of questionnaires and forms as a list of **uniquely named
attributes** for the Datenkatalog. A PDF is rendered page by page to an image and read by a
locally hosted **Gemma 4** vision model (vLLM, OpenAI-compatible); an online form is rendered
in a **browser** and read either from the controls of the rendered page or, where those say
too little, from screenshots of it. A reviewer then checks and corrects the proposed names in
a Streamlit UI.

Only the field **label** is extracted — no data types, no answer options, no mandatory flags.
A checkbox or radio group is therefore a single field, named after its group heading.

## How it works

```
PDF ──► pypdfium2 ─────► one PNG per page ────┐
                                              ├─► Gemma 4 (vLLM) ──► labels + context
URL ──► browser ──┬───► control listing ──────┘                            │
                  └───► screenshot tiles ─────┘  (when the listing says too little)
                                                                           ▼
                                                          ensure_unique_names()  ──► FormField[]
```

Both sources converge on the same two stages, deliberately separated:

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

The sidebar chooses the source — a PDF upload or a form URL — and shows the run summary; the
main area is the work. Two views share one editable table, so an edit in either is visible in
the other:

* **Vergleich mit Quelle** — for a PDF, the rendered page next to the fields found on *that*
  page, one selector driving both. For a web form the pane gives a link to the live form and
  the structure as it was read from the rendered page.
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
make docker-up-web        # the same, plus the browser service for web forms
```

The UI is on <http://localhost:8501>, the API on <http://localhost:8000/docs>.

The browser sits behind a compose profile, so `make docker-up` starts nothing extra and PDF
extraction needs none of it. In production nothing local runs either: `BROWSER_API_URL` points
at the cluster deployment.

Without Docker, against an already-running vLLM:

```bash
make dev      # API on :8000
make dev-ui   # UI on :8501
```

## Web forms

An online form is **rendered in a browser this repository owns** (`docker/browser`, Playwright
plus about a hundred lines) and read from the rendered page. Rendering is not optional: a
modern form has drawn nothing when its HTML arrives, and a form's structure is visual — a
caption is the text beside the box you type into, a section heading is the larger text above a
run of fields.

One render produces two independent readings.

### The listing, read from the page

The browser reports one line per control, with the label and heading chain that *stand beside
and above it on screen*, measured in pixels:

```
- [text] "Familienname:" (name=vater_familienname)
- [radio-gruppe] "Erziehungsberechtigt" (name=berechtigt) — Optionen: Mutter | Vater
- [tabelle] "Beilagen*" — Optionen: Budget/detaillierte Aufstellung | Offerte/n
- [search] "Website durchsuchen" (name=q) [ausserhalb eines <form>]
```

Where the markup states an association — `label for`, `aria-label`, a wrapping `<label>` — it
is believed, because a page that says what it means is more reliable than any inference. Where
it states none, which is most generated forms, the reading is geometric:

* **A caption** is the nearest text to the left of a control on its line, else directly above
  it, else — for a tick box — to its right. Each piece of text captions at most one control,
  and what disqualifies a distant label is not a pixel limit but *another field standing in
  between*: on a line reading `Vorname [ ] Nachname [ ]`, the first field is what stops
  *Vorname* labelling the second.
* **A heading** is text set larger or heavier than the page's body text, with fields below it
  and none beside it. Levels come from ranking those sizes against each other, so a page that
  never uses an `<h1>` still yields a heading chain.
* **A menu is neither.** Text that is nothing but links, or sits in a `nav`/`header`/`footer`
  landmark, is navigation — the rule that keeps a documentation site's sidebar out of
  `context_path`.
* **A control need not be an `<input>`.** ARIA roles count where they wrap no native control,
  so a `role="grid"` that the applicant fills in row by row is one field, with its rows listed
  as options.
* **Every frame is read**, so a form embedded from a form provider counts as part of the page.

Reducing the page this way typically shrinks the prompt by two to three orders of magnitude,
and the `[ausserhalb eines <form>]` marker is emitted only where most controls *are* inside a
form — on a page built without them it would mark every real field and mean nothing.

### The screenshots, when the listing says too little

The same render is also photographed, in tiles the size of the browser window, following
whichever element actually scrolls — an application shell scrolls an inner pane and leaves the
document one screen tall.

Each tile is one browser window (`BROWSER_VIEWPORT_WIDTH` × `BROWSER_VIEWPORT_HEIGHT`,
1280×1024 by default), tiles overlap by 80 px so no field is cut in half, and at most
`MAX_WEB_UNITS` of them are read. One tile is one model call and one "page" in the result, so a
long form costs several calls where the listing costs one.

Which reading is used is *measured*, not guessed: the share of controls that came away with a
label. Below `WEB_MIN_LABELLED_SHARE` (0.5 by default), or where the page yielded no controls
at all, the tiles go to the same vision agent that reads PDF pages, and the run says so in its
warnings. A field reported on two adjoining tiles — the overlap — is reported once, also stated
in the warnings.

The choice can be overruled: **Als Bild auslesen** in the sidebar, or `force_screenshots: true`
on the URL endpoints, reads the screenshots whatever the listing looks like. The measurement is
a good proxy and not a certainty — a page can hand every control a plausible caption and still
have them wrong, and only a person looking at the form can tell.

The DOM reading is preferred wherever it works: it gives labels verbatim, proves which tick
boxes are one group, and costs one model call for a page rather than one per screen.

A run read from tiles carries them back in its result (`page_images`), and the UI shows the
tile beside the fields read off it. Reopening the live form instead would show a *new* render;
what a reviewer has to check against is the screen the model was actually given.

### What is not attempted

**Only the entry URL is read.** A form spread over several pages is not followed: later steps
usually sit behind a submit that needs valid answers, and the links a crawler can see are as
likely to be navigation as the next step. Where the page looks like one step of several
("Schritt 1 von 3"), the result carries a warning rather than quietly returning a partial
inventory.

**A page is never filled in.** Fields that appear only after an answer is given are not seen.

### Why not Firecrawl

The web path ran on self-hosted Firecrawl first. It returns HTML and, self-hosted, nothing
else: screenshots and `actions` live in Fire Engine, which is cloud-only ([SELF_HOST.md][selfhost],
issues [#1028][i1028] and [#2059][i2059], both closed as not planned), and its playwright
adapter neither requests a screenshot nor accepts one back. Every visual question therefore had
to be answered by inferring from markup — sibling order, nesting depth, inline styles — and
those inferences fit the page they were written against and broke on the next toolkit.

Owning the browser answers those questions directly, and replaced five containers (api, redis,
rabbitmq, postgres, playwright) with one. Two Firecrawl findings still hold for anything that
fetches a page: `formats: ["markdown"]` is useless here because markdown has no syntax for a
form control, and an HTTP 200 from a scraping service says nothing about what the page
answered.

[selfhost]: https://github.com/firecrawl/firecrawl/blob/main/SELF_HOST.md
[i1028]: https://github.com/firecrawl/firecrawl/issues/1028
[i2059]: https://github.com/firecrawl/firecrawl/issues/2059

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/extraction/form-fields` | Upload a PDF, get the full inventory. Blocks until done. |
| `POST` | `/extraction/form-fields/stream` | Same, as SSE: a `progress` event per page, then one `result`. |
| `POST` | `/extraction/form-fields/url` | Read an online form. Body: `{"url": "..."}`. |
| `POST` | `/extraction/form-fields/url/stream` | Same, as SSE, with the same event contract. |
| `GET` | `/health/{liveness,readiness,startup}` | Kubernetes probes; readiness checks the LLM. |

```bash
curl -F file=@data/example.pdf http://localhost:8000/extraction/form-fields

curl -X POST http://localhost:8000/extraction/form-fields/url \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.example.ch/anmeldung"}'
```

A URL the backend will not fetch is rejected with **400** before anything is scraped: only
`http` and `https` are accepted, and every address the host resolves to is checked against the
loopback, private, link-local and reserved ranges. A public name whose A record points at
`127.0.0.1` is caught, since the resolved addresses are checked rather than the string.

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

Excel is planned. Everything after extraction is source-agnostic, so:

1. Add a source type to the `ExtractionSource` union in `models/extraction.py`, unless an
   existing one fits.
2. Implement the `FieldExtractor` protocol in `services/extractors/` — `supports()`, which
   receives the source itself, plus an `extract()` that yields one `PageResult` per unit of
   work.
3. Add it to `Container.extractor_registry`.

Naming, uniqueness, the API contract and the UI need no changes. Only extractors know about
modality, which matters because Excel and HTML are not naturally images and should not be
forced through the rendering path.

Dispatch is on the source object rather than a media type string, which is what lets a URL —
having no bytes and no media type — be a first-class source instead of a special case
smuggled through as `text/uri-list`.

A `PageResult.page` is a *unit of work*, not necessarily a page: a PDF page, or one chunk of
a web form too large for the model's context. The UI calls these "Seite" and "Teil"
accordingly.

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


# Datenkatalog Attribute Extractor

Extracts the input fields of questionnaires and forms as a list of **uniquely named
attributes** for the Datenkatalog. A PDF is rendered page by page to an image and read by a
locally hosted **Gemma 4** vision model (vLLM, OpenAI-compatible); an online form is rendered
in a **browser**, walked through its steps, and read from pictures of it, a few consecutive
screens per call. A reviewer then checks and corrects the proposed names in a Streamlit UI.

Only the field **label** is extracted — no data types, no answer options, no mandatory flags.
A checkbox or radio group is therefore a single field, named after its group heading.

## How it works

```
PDF ──► pypdfium2 ──► one PNG per page ──────────────┐
                                                     ├─► Gemma 4 (vLLM) ──► labels + context
URL ──► browser ──► step 1..n ──► screens, 4 per call ┘         ▲                  │
                    (fill required, press Weiter)      labels of those screens     │
                                                       as a spelling reference     ▼
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
plus a few hundred lines), walked through its steps, and read from **screenshots** of it.
Rendering is not optional: a modern form has drawn nothing when its HTML arrives, and a form's
structure is visual — a caption is the text beside the box you type into, a section heading is
the larger text above a run of fields, and a column header applies to the fields under it.

### Read from the picture, spelled from the page

The picture is the reading. An earlier version read the DOM instead, as one line per control
with the heading chain measured off the layout, and fell back to screenshots only where that
said too little. On real cantonal forms the listing lost exactly what matters: the fourteen
tick boxes of *Veranstaltungsart* read as fourteen fields, and every heading on the page
collapsed into one. Both are plain to see in a screenshot.

What the DOM is still good for is spelling. Beside each screen the model gets the labels the
browser measured off that same render, marked as a spelling reference:

```
Wörtliche Beschriftungen der Bedienelemente auf den obigen Bildschirmen …
- [text] Bezeichnung der Verantstaltung
- [checkbox] Anwohnerstrassenfest
- [radio] kommerzieller Anlass
```

It says nothing about what a field is or how fields group — the image decides that. It stops
`AHV-Nummer` coming back as `AHV Nummer` and `Grösse` as `Groesse`, which is what an 11px
label rendered into a screenshot otherwise invites.

### Screens, several per call

The page is photographed in tiles the size of the browser window, following whichever element
actually scrolls — an application shell scrolls an inner pane and leaves the document one
screen tall. Each tile is one browser window (`BROWSER_VIEWPORT_WIDTH` ×
`BROWSER_VIEWPORT_HEIGHT`, 1280×1024 by default), tiles overlap by 80 px, and at most
`MAX_WEB_UNITS` of them are read across all steps.

**`WEB_SCREENS_PER_CALL` (4) consecutive screens go into one call**, because a form does not
break where a screen does. Shown a single tile, the model reports the fragments at its edges as
fields: a caption whose box was cut off below, a box whose caption was cut off above. Shown the
run, it sees one field — and the prompt tells it that the screens overlap and that a field on
two of them is reported once. Screens are never grouped across a step boundary; two steps are
two different pictures of the form.

The ceiling is the served model's: vLLM refuses a request carrying more images than
`--limit-mm-per-prompt` allows, which is why `LLM_IMAGES_PER_PROMPT` (4) and
`WEB_SCREENS_PER_CALL` are separate settings and the second must not exceed the first. Each
image also costs `LLM_IMAGE_SOFT_TOKENS` of context.

One call is one "page" in the result and carries all of its screens back (`page_images`, which
may hold several entries per unit); the UI stacks them beside the fields read off them.
Reopening the live form instead would show a *new* render, of the first step at that; what a
reviewer has to check against is what the model was actually given.

### Multi-step forms

A cantonal form is usually a wizard. `WSU_KESB_103_Gefaehrdungsmeldung` has four steps and
shows five controls on the first; the eGov *Veranstaltung auf öffentlichem Grund* has eight.
Reading the entry page alone returns a fraction of the inventory that looks like the whole
thing, which is the worst answer available.

So the browser walks the form. Per step it photographs what is there, then:

1. answers what the page marks required (`required`, `aria-required`, `aria-invalid`) and
   every radio group, with placeholder values, repeating up to three times because
   requiredness moves as answers are given;
2. presses the page's own *next* button;
3. if the step did not change, fills and presses **again**, up to three times in all — a
   server-validated form says which fields it wanted only *after* refusing, so the second
   round knows strictly more than the first;
4. observes, and stops when the step still does not change.

Filling a real government form is where the time went. Seven things, nearly all silent — the
field ends up empty and the step blames it a round later:

- **`element.value = …` does not fill a React form.** The framework owns the value and never
  learns of the assignment, so the eGov wizard's *Weiter* stays `disabled` while the field
  looks filled. Values go in through Playwright.
- **Requiredness is not always stated.** The KESB form marks nothing at all, not even after
  refusing to advance, which is why every radio group is answered and not only marked controls.
- **A masked field drops what does not fit.** `PLZ` takes digits, so `Test` reads back as
  empty. Candidates are tried in turn and the value is read back; no mask is understood, only
  covered.
- **An autocomplete refuses to be filled and commits only what it recognises.** `Ort`, `PLZ`
  and `Strasse` are jQuery UI autocompletes: `fill` is treated as a programmatic change and
  wiped *after* it returned, and a typed value that is not a real place is cleared when focus
  leaves. They are typed key by key and the suggestion the widget offers is clicked. Clicked,
  never `Enter` — Enter in a text field inside a `<form>` triggers implicit submission.

- **A masked field is never empty.** `Beginn` holds `__:__`, so it looks answered and is
  skipped. A value made only of mask characters counts as none, as does a value the field
  marks `aria-invalid`.
- **The first keystroke into a mask is eaten** moving the caret: typing `1000` into `__:__`
  gives `_0:00`, which is rejected while looking filled in. `Home` first, and the same
  keystrokes give `10:00`.
- **A custom dropdown is not clicked where its input is.** React-Select parks a one-pixel
  `dummyInput` off screen and draws the control as divs around it, so clicking the input fails
  with "Element is outside of the viewport". The nearest ancestor with a rendered box is
  clicked instead — the same rule `observe.js` uses to find where a control sits — and the
  first option offered is taken. Where the dropdown searches instead of just opening, short
  prefixes (`ba`, `st`, …) are typed to make it offer something.

A field that accepts none of the candidates is marked and left alone for the rest of the step,
so three stubborn address fields do not cost minutes of retyping on every round.

When a step still will not advance, the run says which fields it was waiting for
(`blocked_by` → a warning): "The form would not accept: Strasse, Beginn". A next button that is
present but greyed out is reported as **blocked**, never as "no further step button was
found" — the second describes a form that has ended, and that is the difference between a
complete inventory and a quarter of one.

**A submit is never pressed.** Anything reading `absenden`, `abschicken`, `einreichen`,
`senden`, `bestellen`, `kostenpflichtig`, `zahlungspflichtig` and the like is skipped, checked
before the *next* test so that `Weiter zur zahlungspflichtigen Bestellung` is left alone. A
form service takes a filled-in form at its word, and a dummy report to the child protection
authority is not an acceptable cost of reading its field list.

The walk is capped at `WEB_MAX_STEPS` (10), never leaves the site it started on, and closes any
tab the form opens. Where it could not get past a step, the result says so and the inventory is
knowingly partial. `follow_steps: false` on the URL endpoints — **Mehrstufige Formulare
durchklicken** in the sidebar — reads the first step only.

### What is not attempted

**Nothing beyond the form's own steps.** Links into the wider site are not followed, and a
second form linked from the first is a second run.

**Conditional fields behind an optional answer** are not sought out: only what a step demands
is filled, so a section that appears after ticking an optional box is not seen.

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

A `PageResult.page` is a *unit of work*, not necessarily a page: a PDF page, or one screen of
one call over a run of a web form's screens. The UI calls these "Seite" and "Teil".

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


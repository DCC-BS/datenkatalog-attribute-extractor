# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
make install            # uv sync --all-groups
make check              # uv lock --locked, ruff format, ruff check --fix, ty check (this is the CI gate)
make test               # unit tests only
make test-integration   # needs .env + a live vLLM; excluded from CI
make dev                # API on :8000
make dev-ui             # Streamlit UI on :8501
make docker-up          # full stack (vLLM + API + UI) via docker-compose.dev.yml
make docker-up-web      # the same plus the browser service (`web` compose profile)
make eval               # extraction quality against evals/cases; needs a live LLM
make bootstrap-case PDF=data/x.pdf   # seed an eval case skeleton from AcroForm widgets
make env-example        # regenerate .env.example from AppConfig
```

Single test: `PYTHONPATH=src uv run python -m pytest tests/unit/test_naming.py::test_name -v`.
`PYTHONPATH=src` and `--env-file .env` matter — the Makefile sets both; commands run by hand
without them fail on imports or on `init_logger()`, which requires `IS_PROD`.

Eval subset: `PYTHONPATH=src uv run --env-file .env python -m datenkatalog_attribute_extractor_tools.run_extraction_eval --case-id example`.

`make env-example` prints to stdout and does **not** write the file — the generator emits only
AppConfig fields with `TODO` placeholders, so writing it in place destroys the hand-maintained
logging and compose sections.

## Architecture

Two pipelines converging on one naming pass:

- **PDF** → `pypdfium2` renders one PNG per page → vision LLM (Gemma 4 on vLLM) → labels +
  enclosing headings.
- **URL** → our own browser (`docker/browser`) renders the page → `observe.js` reports one line
  per control with the caption and heading chain measured off the rendered layout → text LLM.
  The same render is photographed in viewport-sized tiles (80 px overlap, capped at
  `MAX_WEB_UNITS`, one call per tile); when the observation is mostly unlabelled — below
  `WEB_MIN_LABELLED_SHARE` — those tiles go to the *vision* agent instead, the same one the PDF
  path uses. `UrlSource.force_screenshots` (UI toggle, `force_screenshots` on the URL
  endpoints) demands that reading outright. A tile that was read comes back with the fields
  (`PageResult.image` → `ExtractionResponse.page_images`, base64 PNG) and is what the UI shows
  beside them: the live page reopened is a *fresh* render, not the one the model was given.

Both then go through `services/naming.py`, which assigns document-wide unique names.

**The model never names fields.** It reports each label verbatim plus its `context_path`
(enclosing headings). Uniqueness is a document-wide property a page-scoped call cannot see,
so `ensure_unique_names()` does the disambiguation deterministically: innermost distinguishing
heading first, then more headings, then page number, then a numeric suffix. Do not move this
into the prompt — it would make the core feature untestable.

Every field carries `name` (snake_case slug) and `display_name` (readable). `display_name` is
built from the *label*, never by un-slugging `name`: `AHV-Nummer` must not come back as
"Ahv Nummer", `Grösse` not as "Groesse".

### Layers

- `models/extraction.py` — `ExtractionSource = UploadSource | UrlSource`. Dispatch is on the
  source object, not a media type string, so a URL (no bytes, no media type) is a first-class
  source rather than a special case. `PageResult.page` is a *unit of work*: a PDF page or one
  chunk of a web form.
- `services/extractors/protocol.py` — `FieldExtractor` protocol + `ExtractorRegistry`. The
  extension point for new source kinds (Excel). Implement `supports()` (which receives the
  source) + an `extract()` that yields one `PageResult` per unit of work, add it to
  `Container.extractor_registry`. Nothing downstream changes — naming, API contract and UI are
  source-agnostic, and only extractors know about modality.
- `services/extraction_service.py` — orchestration; knows nothing about PDFs. Validates size,
  resolves an extractor, drains its per-page results, runs the naming pass. `extract()` is
  `extract_streaming()` drained, so both paths share one implementation.
- `services/agents/form_field_agent.py` / `web_field_agent.py` — the vision and text agents.
  `output_type=NativeOutput(PageExtraction)` is load-bearing in both: a bare
  `output_type=PageExtraction` makes pydantic-ai request the result as a tool call, which vLLM
  rejects (`400 tool_choice="required" requires --tool-call-parser`). `NativeOutput` uses
  guided JSON decoding instead. `max_tokens` is deliberately unset.
- `services/llm_health.py` — `is_fatal_llm_error()` splits failures into fatal and page-local.
  `served_context_tokens` exposes the provider's `max_model_len` so prompts can be sized to
  the context actually being served.
- `services/web/` — `url_policy.py` (SSRF guard), `browser_client.py` (render + observe),
  `controls.py` (observations → grouped fields → listing), `chunking.py` (fit to context).
  The reading of the *page* happens in `docker/browser/observe.js`, in the browser, because
  every association a form relies on is visual. Stated markup wins where it exists; otherwise
  a caption is the nearest text left of / above / right of a control, a heading is text set
  larger or heavier with fields below it, and links and `nav` landmarks are neither. Controls
  include ARIA widget roles that wrap no native control, so a `role="grid"` filled in row by
  row is one field with its rows as options.
  The `[ausserhalb eines <form>]` marker is emitted only where most controls are inside a
  form; on a div-built page it would otherwise mark every real field as page furniture and the
  model would discard the whole form.
- `container.py` — dependency-injector wiring; `config` is a `Singleton` (not `Object`) so
  importing the container in tests/tooling/UI does not require a full environment.
- `ui/` — Streamlit, a thin HTTP client over the streaming endpoint. The API stays independently
  usable. UI text is German.

### Failure model

Fatal vs page-local is a deliberate split, because extraction costs minutes.

**Fatal** — before a single page is rasterised, the health probe checks the LLM answers, serves
the configured model, and serves it with `>= LLM_MIN_CONTEXT_TOKENS` context. Uses its own short
`LLM_HEALTH_TIMEOUT` (5s) rather than `LLM_TIMEOUT` so a *hanging* server is caught in seconds.
Any failure aborts with 503. Same if the LLM dies mid-run. Without this, an unavailable model
produced an empty inventory with one warning per page — a wrong answer dressed as a success.

**Page-local** — an unreadable page or unparseable response yields an empty `PageResult` plus a
warning; remaining pages still produce output.

The app does *not* refuse to start when the LLM is down; that is what `/health/readiness` is for.

Pages run sequentially by default. `LLM_MAX_CONCURRENCY` and the vLLM `--max-num-seqs` must be
raised together — raising only the first just queues inside the server.

The web path keeps the same split: URL policy, then LLM health, then the render — everything
that can fail for the whole run fails before any model call. The browser service being down is
fatal (503); a page that will not load is a 400, because it describes the request.

### The browser service (`docker/browser`)

One endpoint, `POST /observe`, returning `{status, title, text, controls[], screenshots[]}`.
Playwright on `mcr.microsoft.com/playwright`, no framework. Things that cost time to find out:

- **`page.evaluate(string)` does not call the string.** Current Playwright evaluates it as an
  expression and returns `undefined`. The collector is installed with `addInitScript` instead,
  which also sidesteps the page's CSP.
- **A screenshot `clip` below the fold needs `fullPage: true`**, or it fails outright with
  "Clipped area is either empty or outside the resulting image".
- **Scrolling the window is not always scrolling the form.** An app shell scrolls an inner
  pane and leaves the document one viewport tall; tiles follow the element with the largest
  scrollable area (`__formScrollTo`).
- **A zero-height element can still be a control.** A virtualised grid is a 0px `<table>`
  inside the box a person sees, and a styled upload is an invisible `<input>` behind a button:
  the rendered box comes from the nearest ancestor that has one. `display: none` (no
  `offsetParent`) stays excluded — a collapsed section is not a field until it is opened.
- **A caption is often several text nodes.** `Ich habe die <a>Wegleitung</a> gelesen` is three;
  they are merged when they share a line *and a block*, which is what stops a label merging
  with the next column.
- **Every frame is observed, not just the main one.** An embedded form provider puts the whole
  form in an iframe; a page-level query finds nothing there.
- **Without a render wait the page is its loading shell.** Measured on the Vaadin form:
  3000 ms shell, 5000 ms rendered, hence `BROWSER_WAIT_MS=8000`.
- **`observe.js` is not unit-tested in CI.** `tests/unit` runs against saved observations in
  `tests/fixtures/*.observation.json`; the collector itself is covered by
  `tests/integration/test_observe_js.py` (`RUN_BROWSER_TESTS=1`, needs `make docker-up-web`).
  Change the collector and those fixtures have to be recaptured, or the unit tests describe a
  browser that no longer exists.

### Context budgeting

Production serves 250k tokens, the dev box 16384, so a fixed prompt size is wrong in one of
them. `chunking.py` sizes the budget from the probe's `served_context_tokens`: one call when
the listing fits, split on section boundaries when it does not, never truncated. Every chunk
repeats its heading chain, because `context_path` is what disambiguates `Familienname` under
*Vater* from the one under *Mutter* — a chunk that lost its headings would silently change the
output. In practice the control listing reduces a page 50-400x, so real forms are one call
even on the dev box.

## Conventions

`.agents/skills/dcc-coding/references/python.md` holds the full DCC Python standard. Load it
before writing non-trivial code. Highlights that bind here:

- Python 3.13, ruff line-length 120, Google-style docstrings on every module/class/function,
  documenting Args/Returns/Raises. Type hints everywhere.
- Reuse `dcc-backend-common` (logger, config, health probes, error handling, `BaseAgent`)
  rather than reimplementing.
- Structured logging: `logger.info("event_name", key=value)`, never f-strings. Mask secrets with
  `log_secret()`.
- Config lives in `utils/app_config.py`; `get_env_or_throw()` for required vars, `os.getenv()`
  with a default otherwise. Adding a setting means updating `from_env`, `__str__`, the compose
  env block, and `make env-example`.
- Tests: `tests/unit` (CI) vs `tests/integration` (local, live LLM or live browser). Minimal
  mocking; build data with `tests/factories.py`. `asyncio_mode = "auto"`, so no
  `@pytest.mark.asyncio` needed. HTTP is faked with `httpx.MockTransport` injected via a
  `transport=` parameter, not by patching. Web unit tests run against saved observations in
  `tests/fixtures/*.observation.json` — deterministic, offline; the HTML beside them is what
  the integration tests re-render to check the observations still hold.
- Dependency cooldown is on (`exclude-newer = "1 week"`); `uv lock --locked` runs in `make check`.

## Eval cases

`make bootstrap-case` seeds from AcroForm widgets, but **labels must be curated by hand**. In
`data/example.pdf` 77 of 81 text widgets are named `Text1`…`Text31` with no tooltip, and every
checkbox *option* is its own widget although the spec counts the group as one field. Treat widget
counts as a sanity check, not an answer key. The eval fails outright on any duplicate name.

Only the field **label** is extracted — no data types, answer options, or mandatory flags. A
checkbox or radio group is one field, named after its group heading.

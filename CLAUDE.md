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
make env-example        # print the AppConfig section of .env.example
```

Single test: `PYTHONPATH=src uv run python -m pytest tests/unit/test_naming.py::test_name -v`.
`PYTHONPATH=src` and `--env-file .env` matter — the Makefile sets both; commands run by hand
without them fail on imports or on `init_logger()`, which requires `IS_PROD`.

Eval subset: `PYTHONPATH=src uv run --env-file .env python -m datenkatalog_attribute_extractor_tools.run_extraction_eval --case-id example`.

`make env-example` prints to stdout and does **not** write the file: the generator emits only
AppConfig fields with `TODO` placeholders, so writing it in place destroys the hand-maintained
logging and compose sections. It defaults to writing `.env.example` itself, which is why the
target redirects it with `-o` to a temporary file — do not call the generator directly.

## Architecture

Two pipelines converging on one naming pass:

- **PDF** → `pypdfium2` renders one PNG per page → vision LLM (Gemma 4 on vLLM) → labels +
  enclosing headings.
- **URL** → our own browser (`docker/browser`) renders the form, walks it step by step, and
  photographs each step in viewport-sized tiles (80 px overlap, capped at `MAX_WEB_UNITS`
  across all steps) → vision LLM, `WEB_SCREENS_PER_CALL` (4) consecutive tiles per call.
  Beside them go the labels `observe.js` measured off those same screens, as a *spelling
  reference only* — the image decides what a field is and how fields group.

  Several tiles per call is load-bearing, not an optimisation: shown one tile, the model
  reports the fragments at its edges as fields — a caption whose box was cut off below, a box
  whose caption was cut off above. Tiles are never grouped across a step. The ceiling is
  vLLM's `--limit-mm-per-prompt` (`LLM_IMAGES_PER_PROMPT`), which rejects a request with more
  images than it allows, so `WEB_SCREENS_PER_CALL` must not exceed it. `UrlSource.follow_steps` (UI toggle,
  `follow_steps` on the URL endpoints) turns the walk off. A tile comes back with the fields
  (`PageResult.image` → `ExtractionResponse.page_images`, base64 PNG) and is what the UI shows
  beside them: the live page reopened is a *fresh* render of step one, not what the model saw.

  There is no DOM-only reading any more, and reinstating one is a step backwards: on the
  cantonal forms it turned the fourteen tick boxes of *Veranstaltungsart* into fourteen fields
  and collapsed every heading on the page into one, both of which a screenshot shows plainly.

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
  source rather than a special case. `PageResult.page` is a *unit of work*: a PDF page, or one
  screen of one step of a web form.
- `services/extractors/protocol.py` — `FieldExtractor` protocol + `ExtractorRegistry`. The
  extension point for new source kinds (Excel). Implement `supports()` (which receives the
  source) + an `extract()` that yields one `PageResult` per unit of work, add it to
  `Container.extractor_registry`. Nothing downstream changes — naming, API contract and UI are
  source-agnostic, and only extractors know about modality.
- `services/extraction_service.py` — orchestration; knows nothing about PDFs. Validates size,
  resolves an extractor, drains its per-page results, runs the naming pass. `extract()` is
  `extract_streaming()` drained, so both paths share one implementation.
- `services/agents/form_field_agent.py` / `web_field_agent.py` — both vision agents, one per
  prompt: a page of a PDF and a screen of a website are not the same thing to read, and the
  web one has to exclude menus, cookie banners and step indicators and to place the label
  reference it is handed. `output_type=NativeOutput(PageExtraction)` is load-bearing in both: a bare
  `output_type=PageExtraction` makes pydantic-ai request the result as a tool call, which vLLM
  rejects (`400 tool_choice="required" requires --tool-call-parser`). `NativeOutput` uses
  guided JSON decoding instead. `max_tokens` is deliberately unset.
- `services/llm_health.py` — `is_fatal_llm_error()` splits failures into fatal and page-local.
  `served_context_tokens` exposes the provider's `max_model_len` so prompts can be sized to
  the context actually being served.
- `services/web/` — `url_policy.py` (SSRF guard), `browser_client.py` (render + walk + tiles),
  `tile_hints.py` (a screen's controls → the label reference sent beside its picture).
  The reading of the *page* happens in `docker/browser/observe.js`, in the browser, because
  every association a form relies on is visual. Stated markup wins where it exists; otherwise
  a caption is the nearest text left of / above / right of a control, a heading is text set
  larger or heavier with fields below it, and links and `nav` landmarks are neither. Controls
  include ARIA widget roles that wrap no native control, so a `role="grid"` filled in row by
  row is one field with its rows as options.
  What that observation is now *for* is narrow: the wording of the labels on one screen. It no
  longer groups anything (`controls.py` grouped tick boxes by shared `name`; the image does it
  better) and no longer has to fit a context (`chunking.py`) — both are deleted.
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

A walk that could not get past a step is neither: it yields a real but partial inventory, and
`stopped_because` becomes a warning on the first result. Silence there would be the worst
outcome available — a fifth of the fields, looking exactly like the whole form.

### The browser service (`docker/browser`)

One endpoint, `POST /observe`, returning
`{status, title, steps[{index, label, controls[], tiles[{image, control_indices}]}], stopped_because}`.
Playwright on `mcr.microsoft.com/playwright`, no framework. Things that cost time to find out:

- **`page.evaluate(string)` does not call the string.** Current Playwright evaluates it as an
  expression and returns `undefined`. The collector is installed with `addInitScript` instead,
  which also sidesteps the page's CSP.
- **A government form's CSP also blocks `new Function`.** Passing a predicate into the page as
  source silently evaluated to nothing on jaxforms, so the autofill filled zero fields and the
  walk stopped at step one with no error anywhere. Anything running in the page is written out
  inline in the `evaluate` callback.
- **Assigning `element.value` does not fill a React form.** The framework owns the value and
  never learns of the assignment: the field looks filled, validates as empty, and the eGov
  wizard's *Weiter* stays `disabled`. Values go in through Playwright's own `fill`/`check`.
- **Requiredness is not always stated, and moves.** The eGov form marks it with `aria-required`
  and flips it as answers are given, so filling runs up to three rounds; the KESB form marks
  nothing at all on first render — which is why *every radio group* is answered, not only
  marked controls.
- **A server-validated form says what it wanted only after refusing.** jaxforms marks `Ort`,
  `PLZ` and `Strasse` `aria-invalid` only once *Weiter* has been pressed and rejected, so one
  fill-and-press is never enough: `ADVANCE_ATTEMPTS` repeats it, and the second round knows
  strictly more than the first. Without it the walk stops on the step whose requirements had
  just been announced.
- **A masked field silently drops what does not fit** (`PLZ` takes digits, so `Test` reads back
  as `""`), **is never empty** (`Beginn` holds `__:__`, so it looks answered — `MASK_ONLY`),
  and **eats the first keystroke** moving the caret (`1000` → `_0:00`, hence `Home` first).
- **An autocomplete refuses to be filled at all**: `fill` is a programmatic change that jQuery
  UI wipes *after* `fill` returned, and a typed value it does not recognise is cleared when
  focus leaves. `fillText` therefore tries candidates, types key by key, clicks the suggestion
  the widget offers, and reads the value back as the test. Never `Enter` — Enter in a text
  field inside a `<form>` is implicit submission.
- **A custom dropdown is not where its input is.** React-Select's `dummyInput` is one pixel and
  off screen; clicking it fails outright. `clickTarget` walks up to the nearest ancestor with a
  rendered box, exactly as `observe.js` does. Such a widget also keeps its selection *outside*
  `element.value`, so "did it work" is answered by whether an option was taken, not by reading
  the input.
- **A disabled next button is not a missing one.** `findNextButton` reports the two separately;
  the walk waits `NEXT_ENABLE_MS` for it to come alive, widens the fill to every empty control
  (`fillRequired(page, everything)`) on the evidence that the page is unsatisfied, and finally
  stops with `blocked` plus `blocked_by`, the controls the step would not accept.
- **A step button is routinely covered** by a sticky footer, so the click is forced after
  visibility and enabledness have been checked directly.
- **Which controls are on a tile is asked after each scroll**, not computed from the reported
  boxes: the boxes are document coordinates and the tile offset belongs to whichever inner
  pane scrolls, so the two are not comparable. `observe.js` marks each control's box element
  with `data-obs-index`, and a viewport intersection answers the question in either case.
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
- **Never press a submit.** `SUBMIT_LABEL` is checked before `NEXT_LABEL`, so
  `Weiter zur zahlungspflichtigen Bestellung` is left alone. This is not a nicety: the walk
  fills in and advances a *live* government form, and the last button of a wizard sends it.
- **`observe.js` and the walk are not unit-tested in CI.** `tests/unit` works from canned
  observations; the browser itself is covered by `tests/integration/test_observe_js.py`
  (`RUN_BROWSER_TESTS=1`, needs `make docker-up-web`), which also checks that the saved pages
  in `tests/fixtures/*.observation.json` still observe the same way.

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
  `transport=` parameter, not by patching. Web unit tests work from canned walks built in the
  test file — deterministic, offline; `tests/fixtures/*.html` and the observations beside them
  are what the integration tests re-render to check the browser still reads them the same way.
- Dependency cooldown is on (`exclude-newer = "1 week"`); `uv lock --locked` runs in `make check`.

## Eval cases

`make bootstrap-case` seeds from AcroForm widgets, but **labels must be curated by hand**. In
`data/example.pdf` 77 of 81 text widgets are named `Text1`…`Text31` with no tooltip, and every
checkbox *option* is its own widget although the spec counts the group as one field. Treat widget
counts as a sanity check, not an answer key. The eval fails outright on any duplicate name.

Only the field **label** is extracted — no data types, answer options, or mandatory flags. A
checkbox or radio group is one field, named after its group heading.

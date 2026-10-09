/**
 * A one-endpoint browser service: render a URL, walk its steps, photograph each of them.
 *
 * It replaces Firecrawl in this stack. Firecrawl returns HTML and, self-hosted, nothing else —
 * no screenshot, no geometry, no computed styles — so every visual question had to be answered
 * by inferring from markup. This service answers them from the rendered page instead.
 *
 * What it hands back per step is a *picture* of the form plus, for each screen of that picture,
 * which of the observed controls stand on it. The picture is what the model reads; the controls
 * come along as the page's own spelling of the labels on it.
 *
 * A form is frequently not one page. Both of the cantonal form services this was written
 * against are wizards — `Start / Meldende Person / Meldung / Dateiupload` — that reveal one
 * step at a time and refuse to advance until the step validates, so reading only what the
 * first render shows yields a fraction of the inventory and no sign that anything is missing.
 * The walk therefore answers every control on a step — a form's own idea of what is required is
 * routinely written down nowhere — and presses the page's own *next* button. Everything except
 * a tick box, which is where consent lives and which is answered only once the step has refused
 * and only where the markup itself says it is required. It never presses anything that reads like a submit: a form service
 * takes a filled-in form at its word, and a dummy report to the child protection authority is
 * not an acceptable cost of reading its field list.
 *
 * That the walk never presses a submit is, on its own, a promise made by a regular expression
 * over button labels. `installWriteGuard` is what makes it a property of the run instead: every
 * request is held up against `decideRequest` before it leaves the browser, a navigation is only
 * let through when the walk has just pressed something it read as *next*, and anything bound
 * for a submit-shaped path is refused whatever led to it. What was allowed and what was stopped
 * comes back with the observation, so a run can be checked rather than trusted.
 *
 * Most of what follows is about filling in a form built out of widgets rather than inputs, and
 * every one of those lessons was paid for on a live cantonal form: see `fillText`. Where a step
 * cannot be satisfied the walk says so and names the controls it was refused on, because a
 * partial inventory that admits it is usable and one that does not is a wrong answer.
 *
 * Deliberately kept to the standard library plus Playwright: one endpoint, no framework.
 */

import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { chromium } from "playwright";

const PORT = Number(process.env.PORT || 3100);
const VIEWPORT_WIDTH = Number(process.env.VIEWPORT_WIDTH || 1280);
const VIEWPORT_HEIGHT = Number(process.env.VIEWPORT_HEIGHT || 1024);
const NAVIGATION_TIMEOUT_MS = Number(process.env.NAVIGATION_TIMEOUT_MS || 60000);
const MAX_BODY_BYTES = 8 * 1024 * 1024;

// How long one control may take to accept a value before it is given up on. A single stuck
// widget must not spend the whole render budget.
const CONTROL_TIMEOUT_MS = 3000;

// After filling, before looking again: a form that validates as you type needs a moment to
// decide, and whether its next button is enabled is exactly what is being read afterwards.
const SETTLE_MS = 1500;

// Filling is repeated because requiredness moves. Answering "Reservationsantrag: ja" makes
// three more fields required and one no longer so, and only the page knows that.
const FILL_ROUNDS = 3;

// How many times one step may be filled and its next button pressed.
//
// More than one, because a form that validates server-side says which fields it wanted only
// *after* refusing: jaxforms marks `Ort`, `PLZ` and `Strasse` with nothing at all until the
// first Weiter, and answers it with `aria-invalid` and a red banner. Filling once and giving
// up therefore stops on the step whose requirements had just been announced.
const ADVANCE_ATTEMPTS = 3;

// Virtualised content renders on scroll; a screenshot taken too soon catches it blank.
const TILE_SETTLE_MS = 150;

// Typing into a field that looks up what is being typed: how long its lookup may take, how
// long its suggestions are waited for, how often it is asked whether it is done, how fast the
// keys go in, and how long the value is given to survive being committed. Too fast and a
// widget that debounces its lookup never sees the last characters; too patient and three
// address fields that will accept nothing cost minutes.
const AUTOCOMPLETE_SETTLE_MS = 4000;
const AUTOCOMPLETE_MENU_MS = 1500;
const AUTOCOMPLETE_POLL_MS = 200;
const AUTOCOMPLETE_COMMIT_MS = 1200;
const TYPING_DELAY_MS = 30;

// How long a next button that is present but greyed out is given to become pressable. A form
// that validates asynchronously — against a register, or on the server — enables it a moment
// after the last field was answered, and giving up in that moment reports a complete form as
// one that has no further steps.
const NEXT_ENABLE_MS = 6000;
const NEXT_ENABLE_POLL_MS = 500;

// Where a suggestion list puts its entries. `role="option"` is the standard; the rest are what
// the widgets actually found on these forms call theirs.
const SUGGESTION_SELECTOR = '[role="option"], .ui-menu-item, .ui-autocomplete li, .autocomplete-suggestion';

// Passed into the page. The gaps are in CSS pixels, at the viewport width above: how far a
// caption may sit from the control it describes before it is somebody else's caption.
const OBSERVE_OPTIONS = {
  maxOptions: 8,
  maxTextLength: 200,
  maxHeadingLength: 80,
  minControlSize: 4,
  // Wide, because a field between the text and the control is what disqualifies a caption;
  // the limit only stops a label being read from the far side of a page with nothing on it.
  captionMaxLeftGap: 1280,
  captionMaxAboveGap: 60,
  captionMaxRightGap: 60,
};

// What the button that advances a wizard says. Anchored at the start so that `Weitere Angaben`
// — an ordinary section heading — is not read as a step, and `\b` keeps `weiter` from matching
// inside `weitere`.
const NEXT_LABEL = /^(weiter|nächste|next|continue|fortfahren|vorwärts|suivant|avanti|siguiente)\b/i;

// What is never pressed, whatever else it says. Checked before the next test, so a button
// reading `Weiter zur zahlungspflichtigen Bestellung` is left alone.
const SUBMIT_LABEL =
  /(absenden|abschicken|einreichen|senden|submit|abschliess|abschließ|bestellen|kaufen|kostenpflichtig|bezahlen|zahlungspflichtig|endgültig|definitiv|beantragen)/i;

const NON_VALUE_TYPES = new Set(["hidden", "submit", "button", "reset", "image", "file"]);

// How strictly the network is held back while a form is walked. Advancing a step and submitting
// a form are both a button press on a wizard, and only one of them is allowed; the button's
// wording is the only thing telling them apart, and a regex over prose is not a guarantee. These
// levels are the guarantee: whatever is clicked, the request has to get past this to leave.
//
// * `all` — nothing but GET and HEAD leaves, and a page may not navigate itself. A step that a
//   server validates cannot be got past at this level, which is the point: it is what an
//   evidence run uses to find out what a form would have sent.
// * `navigation` — form submissions and page navigations are held to the walk's own presses;
//   the lookups a widget needs (XHR, fetch) go through whatever their method.
// * `off` — nothing is held back. Only the label denylist is then between a walk and a submit.
const WRITE_LEVELS = new Set(["all", "navigation", "off"]);
const DEFAULT_WRITE_LEVEL = "all";

// Refused whatever else says otherwise, at every level but `off`, and *not* excusable by the
// walk having pressed something: this is the one check in the guard that does not trust the
// decision that led to the request. A path is a poorer signal than a button's own wording, but
// it is an independent one, and the two failing together is less likely than either alone.
const SUBMIT_PATH =
  /(submit|absenden|abschicken|einreichen|senden|bestellung|bestellen|checkout|payment|zahlung|bezahlen|order)/i;

// Enough of a run's traffic to reconstruct what it did without the log becoming the response.
const MAX_AUDIT_ENTRIES = 400;
const MAX_AUDIT_URL_LENGTH = 300;

// Values that satisfy a validator without meaning anything. They are typed into a browser we
// own and, because no submit is ever pressed, go no further than the step being read.
const PLACEHOLDER = {
  email: "muster@example.ch",
  tel: "0612345678",
  url: "https://example.ch",
  number: "1",
  date: "2026-06-01",
  "datetime-local": "2026-06-01T10:00",
  time: "10:00",
  month: "2026-06",
  week: "2026-W23",
};

// Tried in turn for a plain text field: a word, a postcode, a town, a date in the German
// order, a time, a bare digit. See `fillText` for why one value is not enough. The postcode
// and the town are as much placeholders as "Test" is, chosen because a field that checks what
// it is given against a register of Swiss addresses accepts them and rejects a made-up word.
const TEXT_CANDIDATES = ["Test", "4051", "Basel", "01.01.2000", "10:00", "1"];

// A field wearing its input mask rather than a value: `__:__` for a time, `TT.MM.JJJJ` for a
// date. `element.value` is not empty, so such a field looks answered and is skipped, and the
// step then refuses to advance because of a field that appears to be filled in.
const MASK_ONLY = /^[\s_.:/-]*$/;

// What is typed into a dropdown that searches. Its options are street names, towns, categories
// — nothing a fixed placeholder matches — so what is offered is a *prefix* long enough for the
// widget to start looking (one character is usually below its threshold) and common enough to
// hit something. Whatever comes back is then picked from the list.
const SEARCH_PREFIXES = ["ba", "st", "an", "10"];

// A field that will take none of the candidates is marked and left alone for the rest of the
// step. Without this, three stubborn address fields are retried on every fill round of every
// attempt, and one step costs minutes of typing into fields that will not have it.
const GIVEN_UP = "data-fill-refused";

// The collector is installed as an init script rather than passed to `evaluate` as a string:
// Playwright evaluates a string as an expression and never calls it, and injecting it with
// `new Function` would be at the mercy of the page's own content security policy. An init
// script runs before the page's own scripts and is not subject to either problem.
const observeFile = await readFile(new URL("./observe.js", import.meta.url), "utf8");
const observeInit = `window.__observeForm = ${observeFile.trim().replace(/;$/, "")};`;

const browser = await chromium.launch({ args: ["--disable-dev-shm-usage"] });

/** Read a JSON request body, refusing anything oversized. */
const readJson = (request) =>
  new Promise((resolve, reject) => {
    let body = "";
    request.on("data", (chunk) => {
      body += chunk;
      if (body.length > MAX_BODY_BYTES) reject(new Error("request body too large"));
    });
    request.on("end", () => {
      try {
        resolve(JSON.parse(body || "{}"));
      } catch (error) {
        reject(error);
      }
    });
    request.on("error", reject);
  });

/**
 * The path and query of a URL, which is where a submit endpoint names itself.
 *
 * The host is deliberately left out: a form served from `bestellungen.example.ch` would
 * otherwise be unreadable in its entirety, and it is the endpoint being *called* that says what
 * a request does.
 */
const pathOf = (url) => {
  try {
    const parsed = new URL(url);
    return parsed.pathname + parsed.search;
  } catch {
    return url;
  }
};

/**
 * Whether one request may leave the browser.
 *
 * Kept apart from the interception itself, and given everything it judges by as arguments, so
 * that what the guard permits is one readable function rather than a property of a walk.
 *
 * `sanctioned` says the walk has just pressed a button it read as *next* and is expecting one
 * navigation to follow. It is deliberately not enough on its own: a press is only ever
 * classified by the wording on the button, so `SUBMIT_PATH` is tested first and answers to
 * nobody. Redirects are allowed outright — a redirect only exists because the request it came
 * from was already let through.
 *
 * @returns `{ allow, reason }` — the reason is recorded whichever way it went.
 */
const decideRequest = ({ level, method, resourceType, isRedirect, url, sanctioned, navigated }) => {
  if (isRedirect) return { allow: true, reason: "redirect_of_allowed" };
  if (level !== "off" && SUBMIT_PATH.test(pathOf(url))) return { allow: false, reason: "submit_path" };
  if (level === "off") return { allow: true, reason: "guard_off" };

  const write = method !== "GET" && method !== "HEAD";
  const document = resourceType === "document";

  if (level === "all" && write) return { allow: false, reason: "write_blocked" };
  if (document) {
    if (!sanctioned) return { allow: false, reason: "unsanctioned_navigation" };
    // At `all`, the form is loaded and then only read. Advancing is impossible at this level
    // anyway — the step's POST never leaves — and a form that advances by GET would otherwise
    // carry its answers out in the query string, which is the one way an evidence run could
    // still send something. One page load, and no second one.
    if (level === "all" && navigated) return { allow: false, reason: "navigation_blocked" };
    return { allow: true, reason: "sanctioned_navigation" };
  }
  return { allow: true, reason: "read" };
};

/**
 * A record of what a walk was allowed to send, and what it was stopped from sending.
 *
 * The point of it is that a run can be checked afterwards rather than trusted: "no submit was
 * pressed" is an assertion about this service's own reasoning, whereas "nothing left the
 * browser for `/submit`" is an observation about the network. It is also how the shape of an
 * unfamiliar form service is learned — which of its steps are validated on the server, and
 * what a press actually sends — without letting any of it through.
 */
const newAudit = (level) => ({
  level,
  step: 0,
  sanctioned: 0,
  navigations: 0,
  allowed: 0,
  blocked: 0,
  entries: [],
});

/** Write one decision into the record, whichever way it went. */
const record = (audit, { method, resourceType, url, allow, reason }) => {
  if (allow) audit.allowed += 1;
  else audit.blocked += 1;
  if (audit.entries.length >= MAX_AUDIT_ENTRIES) return;
  // Allowed reads are the bulk of the traffic and say nothing; everything refused, and
  // everything that was allowed to send, is kept.
  if (allow && method === "GET") return;
  audit.entries.push({
    step: audit.step,
    method,
    resource_type: resourceType,
    url: url.slice(0, MAX_AUDIT_URL_LENGTH),
    allowed: allow,
    reason,
  });
};

/** Permit the one navigation a press of *next* is expected to cause. */
const sanction = (audit) => {
  audit.sanctioned += 1;
};

/**
 * Hold everything leaving this context up against the guard, and write down how it went.
 *
 * Three ways out of a browser, and the guard is worth nothing unless it covers all of them.
 * `context.route` sees ordinary requests. It does *not* see a websocket, and it does not see a
 * request made from inside a service worker — a form service that pushed over either would send
 * whatever it liked with the guard reporting a quiet run. Websockets are refused outright below;
 * service workers are stopped from registering at all when the context is created.
 */
const installWriteGuard = async (context, audit) => {
  await context.route("**/*", async (route) => {
    const request = route.request();
    const method = request.method().toUpperCase();
    const resourceType = request.resourceType();
    const decision = decideRequest({
      level: audit.level,
      method,
      resourceType,
      isRedirect: request.redirectedFrom() !== null,
      url: request.url(),
      sanctioned: audit.sanctioned > 0,
      navigated: audit.navigations > 0,
    });

    // A sanction covers one navigation. Spending it here rather than when the button was
    // pressed means a press that causes no request at all does not leave a permission lying
    // about for whatever the page does next.
    if (decision.reason === "sanctioned_navigation") {
      audit.sanctioned -= 1;
      audit.navigations += 1;
    }

    record(audit, { method, resourceType, url: request.url(), allow: decision.allow, reason: decision.reason });

    if (decision.allow) await route.continue().catch(() => {});
    else await route.abort().catch(() => {});
  });

  // A websocket carries anything in either direction and `context.route` never sees it, so
  // there is nothing to hold up against the guard: at any level but `off` it is closed before it
  // is connected to the server. Reading a form does not need one, and the stacks these forms are
  // built on — Vaadin among them — can push over one.
  if (audit.level !== "off") {
    await context
      .routeWebSocket("**", (ws) => {
        record(audit, {
          method: "WEBSOCKET",
          resourceType: "websocket",
          url: ws.url(),
          allow: false,
          reason: "websocket_blocked",
        });
        ws.close();
      })
      .catch(() => {});
  }
};

// Scrolling the *form*, which is not always scrolling the window. An application shell puts
// its content in an inner pane and leaves the document itself the height of the viewport;
// clipping a full-page screenshot then yields one screen and silently loses the rest. So the
// element that actually scrolls furthest is found and driven, which covers both cases.
const SCROLL_INIT = `window.__formScrollTo = (y) => {
  const scrollable = [document.scrollingElement, ...document.querySelectorAll("*")]
    .filter((element) => element && element.scrollHeight - element.clientHeight > 40)
    .sort((a, b) => b.scrollHeight - b.clientHeight - (a.scrollHeight - a.clientHeight));
  const target = scrollable[0] || document.scrollingElement || document.body;
  target.scrollTop = y;
  return { height: target.scrollHeight, viewport: target.clientHeight || window.innerHeight };
};`;

/** Every element matching a selector, across every frame, as handles paired with their frame. */
const handlesAcrossFrames = async (page, selector) => {
  const found = [];
  for (const frame of page.frames()) {
    const handles = await frame
      .locator(selector)
      .elementHandles()
      .catch(() => []);
    for (const handle of handles) found.push({ frame, handle });
  }
  return found;
};

/**
 * Observe every frame, not just the main one.
 *
 * An embedded form provider — and MDN's own examples — puts the whole form in an iframe, where
 * a page-level query finds nothing at all. The init script runs in each frame, so each is asked
 * separately and the answers joined in frame order. Control indices are frame-local, so each
 * frame's answers are offset by the number of controls already seen, and the same offset is
 * applied when asking that frame which of its controls are on screen.
 */
const observeFrames = async (page) => {
  const observed = [];
  let offset = 0;
  for (const frame of page.frames()) {
    let observation;
    try {
      observation = await frame.evaluate((options) => window.__observeForm(options), OBSERVE_OPTIONS);
    } catch {
      // A frame that navigated away or refuses evaluation is skipped rather than fatal.
      continue;
    }
    observed.push({ frame, offset, observation });
    offset += (observation.controls || []).length;
  }
  return observed;
};

const flattenControls = (observed) =>
  observed.flatMap(({ observation, offset }) =>
    (observation.controls || []).map((control) => ({ ...control, index: control.index + offset })),
  );

/**
 * Which controls stand on the screen currently shown.
 *
 * Asked after each scroll rather than computed from the reported boxes, because the two are
 * not in the same coordinate space whenever an inner pane is what scrolls: the boxes are
 * document coordinates and the tile offset belongs to the pane. A viewport intersection asked
 * of the page is right either way. A frame's own answers are in *its* viewport, so its offset
 * within the outer one is added.
 */
const controlsOnScreen = async (page, observed) => {
  const indices = [];
  for (const { frame, offset } of observed) {
    let origin = { x: 0, y: 0 };
    if (frame !== page.mainFrame()) {
      const element = await frame.frameElement().catch(() => null);
      const box = element ? await element.boundingBox().catch(() => null) : null;
      if (element) await element.dispose().catch(() => {});
      if (!box) continue;
      origin = { x: box.x, y: box.y };
    }

    const found = await frame
      .evaluate(
        ({ x, y, width, height }) =>
          [...document.querySelectorAll("[data-obs-index]")]
            .filter((element) => {
              const rect = element.getBoundingClientRect();
              if (rect.width === 0 && rect.height === 0) return false;
              return rect.bottom + y > 0 && rect.top + y < height && rect.right + x > 0 && rect.left + x < width;
            })
            .map((element) => Number(element.getAttribute("data-obs-index"))),
        { ...origin, width: VIEWPORT_WIDTH, height: VIEWPORT_HEIGHT },
      )
      .catch(() => []);

    for (const index of found) indices.push(index + offset);
  }
  return [...new Set(indices)].sort((a, b) => a - b);
};

/**
 * Photograph the form in viewport-sized slices, recording what each slice shows.
 *
 * A form is often several screens long, and one full-page image of it reaches the vision model
 * as a strip too small to read. Slices keep every tile at the resolution the page was rendered
 * at, and each is a unit of work as far as the rest of the pipeline is concerned. They overlap
 * slightly, so a field split by one cut is whole in the next tile.
 */
const screenshotTiles = async (page, observed, maxTiles, overlap) => {
  const tiles = [];
  const geometry = await page.evaluate(() => window.__formScrollTo(0));
  const step = Math.max(1, geometry.viewport - overlap);

  for (let top = 0; top < Math.max(geometry.height, 1) && tiles.length < maxTiles; top += step) {
    await page.evaluate((offset) => window.__formScrollTo(offset), top);
    await page.waitForTimeout(TILE_SETTLE_MS);
    const image = (await page.screenshot({ type: "png" })).toString("base64");
    tiles.push({ image, control_indices: await controlsOnScreen(page, observed) });
    if (top + geometry.viewport >= geometry.height) break;
  }

  await page.evaluate(() => window.__formScrollTo(0));
  return tiles;
};

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Wait for a control that is off doing something — an autocomplete lookup — to come back. */
const settled = async (handle) => {
  for (let waited = 0; waited < AUTOCOMPLETE_SETTLE_MS; waited += AUTOCOMPLETE_POLL_MS) {
    const busy = await handle
      .evaluate((element) => element.disabled || /loading|busy|pending/i.test(element.className || ""))
      .catch(() => false);
    if (!busy) return;
    await pause(AUTOCOMPLETE_POLL_MS);
  }
};

/**
 * The element to click to operate a control, which is not always the control.
 *
 * A custom dropdown puts an `<input>` of one pixel somewhere off screen and draws the thing a
 * person clicks as divs around it — React-Select's `dummyInput` when the select is not
 * searchable. Clicking the input itself fails outright ("Element is outside of the viewport"),
 * so the nearest ancestor that has a rendered box is used instead, exactly as `observe.js`
 * does when deciding where a control sits on the page.
 */
const clickTarget = async (handle) => {
  const box = await handle
    .evaluateHandle((element) => {
      let node = element;
      for (let step = 0; step <= 4 && node; step += 1) {
        const rect = node.getBoundingClientRect();
        if (rect.width >= 8 && rect.height >= 8) return node;
        node = node.parentElement;
      }
      return element;
    })
    .catch(() => null);
  return (box && box.asElement()) || handle;
};

/** The first entry of a suggestion list that is actually on screen, if one has opened. */
const openSuggestion = async (page) => {
  for (const { handle } of await handlesAcrossFrames(page, SUGGESTION_SELECTOR)) {
    const usable = await handle.evaluate((element) => Boolean(element.offsetParent)).catch(() => false);
    if (usable) return handle;
    await handle.dispose().catch(() => {});
  }
  return null;
};

/**
 * Put something into one text-like control and make it stay there.
 *
 * Three things make this more than one `fill`, all of them met on the KESB form's address
 * block, and all of them silent — the field ends up empty and the step blames it later:
 *
 * A **masked** field drops what does not fit. `PLZ` takes digits, so "Test" reads back as
 * empty. Candidates are therefore tried until one survives, and read-back is the test — no
 * mask has to be understood, only covered.
 *
 * An **autocomplete** field refuses to be filled. `Ort`, `PLZ` and `Strasse` are jQuery UI
 * autocompletes: `fill` puts the value in at once, the widget treats that as a programmatic
 * change, and wipes it — some time *after* `fill` returned, so reading back immediately still
 * says it worked. Typed key by key, the same value is accepted.
 *
 * And such a field **commits only what it recognises**. Typing `Test` into `Ort` and moving on
 * clears it again, because it is not a place; a value survives when it is picked from the list
 * the widget offers. So the list is waited for and its first entry clicked. Clicked, not
 * `Enter`: Enter in a text field inside a `<form>` triggers implicit submission, which is the
 * one thing this service must never do.
 *
 * @returns Whether the control ended up holding a value.
 */
const fillText = async (page, handle, type) => {
  const typed = PLACEHOLDER[type] ? [PLACEHOLDER[type], ...TEXT_CANDIDATES] : TEXT_CANDIDATES;

  const suggests = await handle
    .evaluate(
      (element) =>
        element.getAttribute("aria-autocomplete") !== null ||
        element.getAttribute("role") === "combobox" ||
        element.getAttribute("list") !== null ||
        /autocomplete|typeahead|combobox|token|select2|chosen/i.test(element.className || ""),
    )
    .catch(() => false);

  /** Click the control where a person would, which is what opens a custom dropdown. */
  const open = async () => {
    const target = await clickTarget(handle);
    await target.scrollIntoViewIfNeeded().catch(() => {});
    await target.click({ timeout: CONTROL_TIMEOUT_MS, force: true }).catch(() => {});
    if (target !== handle) await target.dispose().catch(() => {});
  };

  const typeOut = async (candidate) => {
    await handle.fill("", { timeout: CONTROL_TIMEOUT_MS }).catch(() => {});
    await open();
    await handle.focus().catch(() => {});
    // The caret goes to the start first. A masked field puts it wherever the click landed and
    // swallows the first keystroke moving it: typing `1000` into an empty `__:__` yields
    // `_0:00`, which the field then rejects while looking filled in. From the start, the same
    // keystrokes give `10:00`.
    await page.keyboard.press("Home").catch(() => {});
    // Through the keyboard rather than the element: an element handle has no un-deprecated way
    // of typing, and the keyboard reaches whichever frame holds the focus.
    await page.keyboard.type(candidate, { delay: TYPING_DELAY_MS });
    await settled(handle);
  };

  /** Take the first option the widget is offering, if it is offering any. */
  const pick = async () => {
    for (let waited = 0; waited < AUTOCOMPLETE_MENU_MS; waited += AUTOCOMPLETE_POLL_MS) {
      const suggestion = await openSuggestion(page);
      if (suggestion) {
        await suggestion.click({ timeout: CONTROL_TIMEOUT_MS, force: true }).catch(() => {});
        await suggestion.dispose().catch(() => {});
        await pause(AUTOCOMPLETE_COMMIT_MS);
        return true;
      }
      await pause(AUTOCOMPLETE_POLL_MS);
    }
    return false;
  };

  /** Commit what was typed the way the widget expects, and let it settle. */
  const commit = async () => {
    if (await pick()) return;
    // A real focus change, so a widget that validates on blur does so against a real event.
    await page.keyboard.press("Tab").catch(() => {});
    await pause(AUTOCOMPLETE_COMMIT_MS);
  };

  /**
   * What the control is holding, or "" for nothing and for its bare mask.
   *
   * `aria-invalid` is deliberately *not* consulted. It used to count as failure here, on the
   * reasoning that a widget which rejects a value marks itself — but a server-validated form
   * marks the field in the HTML it renders and never unmarks it, because nothing revalidates
   * until the next submission. Every candidate then read back as a failure however well it had
   * gone in: `4051` in a postcode and `10:00` in a time field were both typed successfully, both
   * judged refused, and the field was given up on for the rest of the walk. Whether a value
   * stuck is a question about the value.
   */
  const valueOf = async () => {
    const value = await handle.evaluate((element) => element.value).catch(() => null);
    if (typeof value !== "string") return "";
    return MASK_ONLY.test(value) ? "" : value;
  };

  const candidates = suggests ? [...SEARCH_PREFIXES, ...typed] : typed;

  // A dropdown that offers a list without being typed into — a custom select — is answered by
  // opening it and taking what it offers. Nothing is typed, so nothing has to be recognised.
  if (suggests) {
    await open();
    // Such a control keeps its selection somewhere other than `value`; that an option was taken
    // is the whole answer, and the step will say soon enough if it was not enough. Its
    // `aria-invalid` is not consulted for the same reason `valueOf` does not: on a
    // server-validated form that attribute describes the last submission, not this value.
    if (await pick()) return true;
  }

  for (const candidate of candidates) {
    if (suggests) {
      await typeOut(candidate);
      // Taking an option *is* the answer here, and has to be the test: such a widget clears
      // what was typed once a selection is made, so reading the input back says nothing.
      if (await pick()) return true;
      await commit();
      if (await valueOf()) return true;
      continue;
    }

    await handle.fill(candidate, { timeout: CONTROL_TIMEOUT_MS });
    // Plenty of forms validate on blur and nowhere else.
    await handle.evaluate((element) => element.dispatchEvent(new Event("blur", { bubbles: true }))).catch(() => {});
    if (await valueOf()) return true;

    await typeOut(candidate);
    await commit();
    if (await valueOf()) return true;
  }

  await handle.evaluate((element, marker) => element.setAttribute(marker, "1"), GIVEN_UP).catch(() => {});
  return false;
};

/**
 * Answer every control on the step that is waiting for an answer, once over.
 *
 * *Every* control, from the first attempt, and not only the ones the page marks as required.
 * Marked requiredness was tried first and is not good enough to walk on: the KESB form marks
 * nothing at all — no `required`, no `aria-required`, nothing even after it has refused to
 * advance and said in prose that the field is mandatory — and the eGov wizard's third step will
 * not move until two dropdowns nothing describes are answered. A walk that answers only what it
 * is told to answer stops on the first step of both, and reports a wizard as five fields.
 *
 * Tick boxes are the exception, and `checkboxes` is what lifts it — only once the step has
 * refused to advance, and even then only for boxes the page itself marks as required. A tick
 * box is where consent lives: *Ich akzeptiere die AGB*, *kostenpflichtig bestellen*. Ticking one
 * unasked is this project agreeing to something on somebody's behalf, so it is done only when
 * the form has stated in markup that it is mandatory and has already refused to go on without
 * it. Radio groups are not consent — the form is asking a question, exactly one option ends up
 * set, and nothing is typed — so they are answered from the start.
 *
 * Values go in through Playwright rather than by assigning `element.value`, which a
 * React-controlled input ignores outright — the framework owns the value and never learns of
 * the assignment, so the field looks filled and validates as empty. That is what the eGov
 * wizard does, and its next button stays disabled until the framework itself is convinced.
 */
const fillRequired = async (page, checkboxes = false) => {
  let filled = 0;

  for (const { handle } of await handlesAcrossFrames(page, "input, select, textarea")) {
    const info = await handle
      .evaluate((element) => {
        // What the page itself says must be answered. `aria-invalid` counts because a form
        // that has just refused to advance says so that way, which is the same statement
        // arriving later. Written out here rather than passed in as source: a page with a
        // strict content security policy — which a government form service has — refuses to
        // run `new Function`, and the fill would then silently do nothing at all.
        const invalid = element.getAttribute("aria-invalid") === "true";
        const required =
          element.required ||
          element.getAttribute("aria-required") === "true" ||
          element.closest('[aria-required="true"]') !== null ||
          invalid;
        const type = (element.type || "text").toLowerCase();
        const grouped = type === "radio" || type === "checkbox";
        return {
          tag: element.tagName,
          type,
          name: element.name || "",
          required,
          invalid,
          usable:
            Boolean(element.offsetParent) &&
            !element.disabled &&
            !element.readOnly &&
            !element.hasAttribute("data-fill-refused"),
          value: element.value,
          groupAnswered:
            grouped && element.name
              ? document.querySelector(`input[type=${type}][name="${CSS.escape(element.name)}"]:checked`) !== null
              : element.checked,
          options:
            element.tagName === "SELECT"
              ? [...element.options].filter((option) => option.value && !option.disabled).map((option) => option.value)
              : [],
        };
      })
      .catch(() => null);

    // Everything is answered except a tick box, which waits for the step to have refused and
    // for the page to have said the box is required.
    const wanted = info && (info.type === "checkbox" ? checkboxes && info.required : true);
    if (!info || !info.usable || !wanted || NON_VALUE_TYPES.has(info.type)) {
      await handle.dispose().catch(() => {});
      continue;
    }

    try {
      if (info.type === "radio" || info.type === "checkbox") {
        if (!info.groupAnswered) {
          await handle.check({ timeout: CONTROL_TIMEOUT_MS, force: true });
          filled += 1;
        }
      } else if (info.tag === "SELECT") {
        if (!info.value && info.options.length) {
          await handle.selectOption(info.options[0], { timeout: CONTROL_TIMEOUT_MS });
          filled += 1;
        }
      } else if (
        (!info.value || MASK_ONLY.test(info.value) || info.invalid) &&
        (await fillText(page, handle, info.type))
      ) {
        filled += 1;
      }
    } catch {
      // A control that will not take a value is left empty; the walk stops on its own if that
      // was the one holding the step back.
    }

    await handle.dispose().catch(() => {});
  }

  return filled;
};

/**
 * Forget which controls were given up on.
 *
 * The mark is what stops three stubborn address fields being retyped on every round of every
 * attempt, and it is right to carry it across the attempts on *one* step. Carrying it into the
 * next step is not: a single-page wizard keeps the same elements between steps, and a control
 * refused while the step it belonged to was unsatisfied would then never be offered a value
 * again. Called when a new step is reached.
 */
const forgetFillMarks = async (page) => {
  for (const frame of page.frames()) {
    await frame
      .evaluate(
        (marker) => document.querySelectorAll(`[${marker}]`).forEach((element) => element.removeAttribute(marker)),
        GIVEN_UP,
      )
      .catch(() => {});
  }
};

/**
 * The page's own button for advancing a step.
 *
 * A button that is present but disabled is reported as such rather than as nothing. The two
 * are different situations and the difference is the whole diagnosis: the last step of a
 * wizard has no next button, whereas a step whose *Weiter* is greyed out is a step whose
 * requirements have not been met. Answering the second with "no further step button was found"
 * would describe a complete form.
 */
const findNextButton = async (page) => {
  const selector = 'button, a[href], [role="button"], input[type="submit"], input[type="button"]';
  let next = null;
  let disabled = false;

  for (const { handle } of await handlesAcrossFrames(page, selector)) {
    const info = await handle
      .evaluate((element) => ({
        shown: Boolean(element.offsetParent),
        enabled: !element.disabled && element.getAttribute("aria-disabled") !== "true",
        text: (element.innerText || element.value || element.getAttribute("aria-label") || "")
          .replace(/\s+/g, " ")
          .trim(),
      }))
      .catch(() => null);

    const advances = info && info.shown && info.text && !SUBMIT_LABEL.test(info.text) && NEXT_LABEL.test(info.text);
    if (advances && info.enabled && !next) {
      next = { handle, text: info.text };
      continue;
    }
    if (advances) disabled = true;
    await handle.dispose().catch(() => {});
  }

  return { next, disabled };
};

/**
 * The controls a stuck step is still not happy with, named the way a person would name them.
 *
 * A step that will not advance is the one thing a reviewer has to be able to act on: the
 * inventory stops there, and "it would not advance" is only useful with "because of these".
 * Reported for the same reason the walk reports why it stopped at all.
 */
const unsatisfiedControls = async (page) => {
  const names = [];
  for (const frame of page.frames()) {
    const found = await frame
      .evaluate((maskSource) => {
        const mask = new RegExp(maskSource);
        const labelOf = (element) => {
          const stated =
            (element.id && document.querySelector(`label[for="${CSS.escape(element.id)}"]`)) ||
            element.closest("label");
          const text = stated ? stated.innerText || stated.textContent : "";
          return (text || element.getAttribute("aria-label") || element.name || element.id || "")
            .replace(/\s+/g, " ")
            .trim()
            .slice(0, 60);
        };

        const unsatisfied = [];
        for (const element of document.querySelectorAll("input:not([type=hidden]), select, textarea")) {
          if (!element.offsetParent || element.disabled) continue;
          const type = (element.type || "text").toLowerCase();
          if (type === "radio" || type === "checkbox") {
            const answered = element.name
              ? document.querySelector(`input[type=${type}][name="${CSS.escape(element.name)}"]:checked`) !== null
              : element.checked;
            if (!answered) unsatisfied.push(labelOf(element));
            continue;
          }
          // The control's own word for it, or its immediate wrapper's. The wrapper matters
          // for a widget that keeps no value in the input at all: the eGov street dropdown
          // reads empty, shows no placeholder, and its own box says "Field must not be empty".
          // Only the nearest few ancestors are consulted — a whole section marked invalid says
          // nothing about which field in it is at fault.
          let marked = element.getAttribute("aria-invalid") === "true";
          let wrapper = element.parentElement;
          for (let step = 0; step < 3 && wrapper && !marked; step += 1) {
            marked = /--invalid/.test(wrapper.className || "");
            wrapper = wrapper.parentElement;
          }
          if (marked) {
            unsatisfied.push(labelOf(element));
            continue;
          }
          // A custom dropdown keeps its selection in the widget, not in the input: the
          // `<input>` React-Select hides behind the control reads empty however much has been
          // chosen. What it does show is a placeholder, until something is.
          const combo =
            element.getAttribute("aria-autocomplete") !== null || element.getAttribute("role") === "combobox";
          if (combo) {
            let node = element;
            for (let step = 0; step <= 4 && node; step += 1) {
              const rect = node.getBoundingClientRect();
              if (rect.width >= 8 && rect.height >= 8) break;
              node = node.parentElement;
            }
            if (node && node.querySelector('[class*="placeholder"]')) unsatisfied.push(labelOf(element));
            continue;
          }
          if (!element.value || mask.test(element.value)) unsatisfied.push(labelOf(element));
        }
        return unsatisfied.filter(Boolean);
      }, MASK_ONLY.source)
      .catch(() => []);
    names.push(...found);
  }
  return [...new Set(names)].slice(0, 20);
};

/** What the page says the current step is called, where it says so in a standard way. */
const stepLabel = (page) =>
  page
    .evaluate(() => {
      const current = document.querySelector('[aria-current="step"], [aria-current="page"], [aria-current="true"]');
      return current ? (current.innerText || "").replace(/\s+/g, " ").trim().slice(0, 80) : "";
    })
    .catch(() => "");

/**
 * What the step being looked at consists of, by the *identity* of its controls.
 *
 * Never by the words printed next to them. A step that refuses to advance re-renders with its
 * captions rewritten — `Vorname` becomes `Vorname — Feld darf nicht leer sein` — and read by its
 * labels that is a page full of controls nobody has ever seen. The walk concluded it had
 * advanced, recorded the same step again, and photographed it covered in error banners, which is
 * then what the model is handed as a picture of the form. Names do not move when a form scolds.
 *
 * Duplicates are numbered rather than collapsed, so ten identically named tick boxes are ten
 * keys, and a control appearing *between* two others shifts nothing.
 */
const stepKeys = (controls) => {
  const seen = new Map();
  return controls.map((control) => {
    const key = `${control.kind}|${control.identity || control.name}`;
    const count = (seen.get(key) || 0) + 1;
    seen.set(key, count);
    return `${key}|${count}`;
  });
};

/**
 * Whether two observations are of the same step.
 *
 * Three signals, and the page has to disagree on one of them before this counts as a new step:
 * the address, what the page calls the step it is on, and the controls standing on it.
 *
 * The controls are compared as a *subset*, not for equality, because answering a form reveals
 * more of it: "Reservationsantrag: ja" unfolds three further fields with the step unchanged. A
 * step that has gained controls is the same step; one that has lost a control is not.
 */
const sameStep = (before, after) =>
  before.url === after.url && before.label === after.label && before.keys.every((key) => after.keys.includes(key));

/** Everything that identifies the step now on screen, together with what was observed of it. */
const stepStateOf = async (page) => {
  const observed = await observeFrames(page);
  const controls = flattenControls(observed);
  return { url: page.url(), label: await stepLabel(page), keys: stepKeys(controls), controls, observed };
};

/**
 * Read one step, then try to advance to the next.
 *
 * Returns why it stopped, which the caller reports: a partial inventory that says so is usable,
 * one that does not is a wrong answer dressed as a complete list.
 */
const walkSteps = async (page, { maxSteps, maxTiles, tileOverlap, screenshots, autofill, stepWaitMs, audit }) => {
  const origin = new URL(page.url()).origin;
  const steps = [];
  const seen = new Set();
  let stopped = "max_steps";

  for (let index = 1; index <= maxSteps; index += 1) {
    audit.step = index;
    // A fresh step knows nothing about which controls the last one gave up on.
    await forgetFillMarks(page);
    const state = await stepStateOf(page);
    const { observed, controls } = state;
    const fingerprint = `${state.url}|${state.label}|${state.keys.join(",")}`;

    if (seen.has(fingerprint)) {
      stopped = "unchanged";
      break;
    }
    seen.add(fingerprint);

    steps.push({
      index,
      label: state.label,
      url: state.url,
      title: observed[0] ? observed[0].observation.title : "",
      controls,
      tiles: screenshots ? await screenshotTiles(page, observed, maxTiles, tileOverlap) : [],
      // How this step was got past, reported because a walk that stalls has to be diagnosable
      // from its answer: nothing filled, no button found, and three refusals are three
      // different failures.
      filled: 0,
      advanced_by: "",
      attempts: 0,
      blocked_by: [],
    });

    if (index === maxSteps) break;

    const current = steps[steps.length - 1];
    let advanced = false;

    // Fill, press, and — if the page refused — fill again and press again. The later rounds are
    // not retries of the same thing: a server-validated form answers the first Weiter by marking
    // the fields it wanted, so the second fill knows strictly more than the first. Once the step
    // has refused, the fill also widens to tick boxes the page marks required, which is the only
    // circumstance in which one is ever ticked.
    let blocked = false;

    for (let attempt = 0; attempt < ADVANCE_ATTEMPTS && !advanced; attempt += 1) {
      if (autofill) {
        for (let round = 0; round < FILL_ROUNDS; round += 1) {
          const filled = await fillRequired(page, blocked);
          current.filled += filled;
          if (filled === 0) break;
          await page.waitForTimeout(SETTLE_MS);
        }
      }

      let { next, disabled } = await findNextButton(page);
      // Present but greyed out: wait for it, rather than concluding anything from a state the
      // page is still deciding.
      for (let waited = 0; !next && disabled && waited < NEXT_ENABLE_MS; waited += NEXT_ENABLE_POLL_MS) {
        await pause(NEXT_ENABLE_POLL_MS);
        ({ next, disabled } = await findNextButton(page));
      }

      if (!next) {
        // Nothing to press. Either this is the last step, or the button is there and greyed
        // out — in which case another round of filling may yet enable it.
        blocked = disabled;
        if (!disabled || !autofill || attempt === ADVANCE_ATTEMPTS - 1) {
          stopped = disabled ? "blocked" : "no_next";
          if (disabled) current.blocked_by = await unsatisfiedControls(page);
          break;
        }
        continue;
      }

      current.advanced_by = next.text;
      current.attempts = attempt + 1;

      await next.handle.scrollIntoViewIfNeeded().catch(() => {});
      // The one navigation this press is entitled to. Anything else the page tries — a redirect
      // to a payment page, a script submitting the form behind the click — meets the guard
      // without a permission and is refused.
      sanction(audit);
      // Forced, because a step button is routinely covered by a sticky footer or an overlay
      // and an actionability check then times out on a button a person can plainly press. The
      // element was already established to be visible and enabled.
      await next.handle.click({ timeout: CONTROL_TIMEOUT_MS, force: true }).catch(async () => {
        await next.handle.evaluate((element) => element.click()).catch(() => {});
      });
      await next.handle.dispose().catch(() => {});
      await page.waitForTimeout(stepWaitMs);

      if (new URL(page.url()).origin !== origin) {
        stopped = "left_site";
        break;
      }

      advanced = !sameStep(state, await stepStateOf(page));
      // The step was pressed and stayed put, which is the page saying it is not satisfied — the
      // same statement a disabled button makes, arriving later. From here a tick box the page
      // marks required may be answered.
      if (!advanced) blocked = true;
      // Nothing was filled and the page did not move: pressing again would only repeat itself.
      if (!advanced && !autofill) break;
    }

    if (stopped === "no_next" || stopped === "left_site" || stopped === "blocked") break;
    if (!advanced) {
      stopped = "unchanged";
      current.blocked_by = await unsatisfiedControls(page);
      break;
    }
  }

  return { steps, stopped };
};

const observe = async ({
  url,
  html,
  waitMs = 8000,
  maxTiles = 12,
  tileOverlap = 80,
  screenshots = true,
  maxSteps = 1,
  autofill = true,
  stepWaitMs = 5000,
  blockWrites = DEFAULT_WRITE_LEVEL,
}) => {
  const context = await browser.newContext({
    viewport: { width: VIEWPORT_WIDTH, height: VIEWPORT_HEIGHT },
    deviceScaleFactor: 1,
    // A service worker's requests do not pass through `context.route`, so anything sent from
    // inside one would leave without meeting the guard at all. Nothing about reading a form
    // needs one.
    serviceWorkers: "block",
  });
  const audit = newAudit(WRITE_LEVELS.has(blockWrites) ? blockWrites : DEFAULT_WRITE_LEVEL);
  await installWriteGuard(context, audit);
  await context.addInitScript({ content: observeInit });
  await context.addInitScript({ content: SCROLL_INIT });
  const page = await context.newPage();

  // A wizard that opens a leaflet in a new tab must not leave the walk looking at it, and a
  // confirmation dialog must not block it.
  context.on("page", (opened) => {
    if (opened !== page) opened.close().catch(() => {});
  });
  page.on("dialog", (dialog) => dialog.dismiss().catch(() => {}));

  try {
    // `html` renders markup handed in directly instead of fetching a URL. It exists for the
    // tests: the observation is what has to be pinned down, and a saved page renders the same
    // way every time, with no network and no site that may have changed since.
    let status = 200;
    if (html) {
      await page.setContent(html, { waitUntil: "load", timeout: NAVIGATION_TIMEOUT_MS });
    } else {
      // The run's own navigation, which is as sanctioned as a navigation gets.
      sanction(audit);
      const response = await page.goto(url, { waitUntil: "load", timeout: NAVIGATION_TIMEOUT_MS });
      status = response ? response.status() : 0;
    }

    // A single-page app has drawn nothing when `load` fires; the wait is what makes the
    // difference between an empty shell and the form.
    await page.waitForTimeout(waitMs);
    try {
      await page.waitForLoadState("networkidle", { timeout: 5000 });
    } catch {
      // A page that never goes idle — polling, telemetry, a live map — is still readable.
    }

    const { steps, stopped } = await walkSteps(page, {
      maxSteps: Math.max(1, maxSteps),
      maxTiles,
      tileOverlap,
      screenshots,
      autofill,
      stepWaitMs,
      audit,
    });

    return {
      url: page.url(),
      status,
      title: steps.length ? steps[0].title : "",
      steps,
      stopped_because: stopped,
      network_audit: {
        level: audit.level,
        allowed: audit.allowed,
        blocked: audit.blocked,
        entries: audit.entries,
      },
    };
  } finally {
    await context.close();
  }
};

const server = createServer(async (request, response) => {
  const send = (status, body) => {
    const payload = JSON.stringify(body);
    response.writeHead(status, { "content-type": "application/json", "content-length": Buffer.byteLength(payload) });
    response.end(payload);
  };

  if (request.method === "GET" && request.url.startsWith("/health")) {
    send(200, { status: browser.isConnected() ? "ok" : "disconnected" });
    return;
  }

  if (request.method !== "POST" || !request.url.startsWith("/observe")) {
    send(404, { error: "not found" });
    return;
  }

  try {
    const payload = await readJson(request);
    if (!payload.url && !payload.html) {
      send(400, { error: "url or html is required" });
      return;
    }
    send(200, await observe(payload));
  } catch (error) {
    // Navigation failures describe the requested page, not this service, and the caller has
    // to be able to tell those apart: one is a bad request, the other is an outage.
    const message = String((error && error.message) || error);
    const navigation = /net::|Timeout .* exceeded|ERR_/.test(message);
    send(navigation ? 502 : 500, { error: message, kind: navigation ? "navigation" : "browser" });
  }
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(JSON.stringify({ event: "browser_service_started", port: PORT }));
});

const shutdown = async () => {
  await browser.close().catch(() => {});
  server.close(() => process.exit(0));
};

process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);

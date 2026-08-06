/**
 * A one-endpoint browser service: render a URL, observe its form controls, photograph it.
 *
 * It replaces Firecrawl in this stack. Firecrawl returns HTML and, self-hosted, nothing else —
 * no screenshot, no geometry, no computed styles — so every visual question had to be answered
 * by inferring from markup. This service answers them from the rendered page instead, and
 * hands back both readings of the same render: the control inventory (`observe.js`) and the
 * screenshots, so the caller can fall back to reading the picture when the DOM says too little.
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

/**
 * Photograph the form in viewport-sized slices.
 *
 * A form is often several screens long, and one full-page image of it reaches the vision model
 * as a strip too small to read. Slices keep every tile at the resolution the page was rendered
 * at, and each is a page of the document as far as the rest of the pipeline is concerned. They
 * overlap slightly, so a field split by one cut is whole in the next tile.
 */
const screenshotTiles = async (page, maxTiles, overlap) => {
  const tiles = [];
  const geometry = await page.evaluate(() => window.__formScrollTo(0));
  const step = Math.max(1, geometry.viewport - overlap);

  for (let top = 0; top < Math.max(geometry.height, 1) && tiles.length < maxTiles; top += step) {
    await page.evaluate((offset) => window.__formScrollTo(offset), top);
    // Virtualised content renders on scroll; a screenshot taken too soon catches it blank.
    await page.waitForTimeout(150);
    tiles.push((await page.screenshot({ type: "png" })).toString("base64"));
    if (top + geometry.viewport >= geometry.height) break;
  }

  await page.evaluate(() => window.__formScrollTo(0));
  return tiles;
};

const observe = async ({ url, html, waitMs = 8000, maxTiles = 12, tileOverlap = 80, screenshots = true }) => {
  const context = await browser.newContext({
    viewport: { width: VIEWPORT_WIDTH, height: VIEWPORT_HEIGHT },
    deviceScaleFactor: 1,
  });
  await context.addInitScript({ content: observeInit });
  await context.addInitScript({ content: SCROLL_INIT });
  const page = await context.newPage();

  try {
    // `html` renders markup handed in directly instead of fetching a URL. It exists for the
    // tests: the observation is what has to be pinned down, and a saved page renders the same
    // way every time, with no network and no site that may have changed since.
    let status = 200;
    if (html) {
      await page.setContent(html, { waitUntil: "load", timeout: NAVIGATION_TIMEOUT_MS });
    } else {
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

    // Every frame, not just the main one. An embedded form provider — and MDN's own examples
    // — puts the whole form in an iframe, where a page-level query finds nothing at all. The
    // init script runs in each frame, so each can be asked separately and the answers joined
    // in frame order.
    const observations = [];
    for (const frame of page.frames()) {
      try {
        observations.push(await frame.evaluate((options) => window.__observeForm(options), OBSERVE_OPTIONS));
      } catch {
        // A frame that navigated away or refuses evaluation is skipped rather than fatal.
      }
    }

    const observation = observations[0] || { title: "", text: "", controls: [] };
    const controls = observations.flatMap((entry) => entry.controls || []);
    const tiles = screenshots ? await screenshotTiles(page, maxTiles, tileOverlap) : [];

    return {
      url: page.url(),
      status,
      title: observation.title,
      text: observation.text,
      controls,
      screenshots: tiles,
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

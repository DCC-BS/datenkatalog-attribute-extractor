"""Rendering a form page in a browser we control, and reading what it looks like.

The web path used to fetch HTML from Firecrawl and infer everything else from the markup:
which text was a caption, which was a heading, what counted as a control. Self-hosted
Firecrawl returns nothing but HTML — no screenshot, no geometry, no computed styles — so every
one of those questions was answered by guessing from the DOM, and guesses about DOM shape are
guesses about one toolkit's markup. They held for the page they were written against and broke
on the next one.

This client talks to `docker/browser`, a Playwright service in this repository. It renders the
page once and returns two independent readings of that render:

* an **observation** — the controls, each with the caption and heading chain that stand beside
  and above it on the rendered page, measured in pixels rather than inferred from nesting;
* **screenshots** — the same page in viewport-sized tiles, so that a page the observation
  cannot read (a canvas form, an image of a form) can still be read by the vision model that
  already serves the PDF path.

The page's own HTTP status is reported separately from the service's, for the same reason it
was with Firecrawl: a service that answers 200 says nothing about whether the page did.
"""

import base64
from dataclasses import dataclass, field

import httpx
from dcc_backend_common.logger import get_logger

logger = get_logger(__name__)

SUCCESSFUL_PAGE_STATUSES = range(200, 300)

# The browser service reports a failed navigation as 502 with `kind: navigation`: the page did
# not load, which describes the request rather than the service.
NAVIGATION_FAILURE_STATUS = 502

# Added to the render deadline so the service's own timeout expires first and answers with a
# message, rather than the client giving up on a request that is still being served.
HTTP_TIMEOUT_HEADROOM_SECONDS = 15


class BrowserUnavailableError(RuntimeError):
    """Raised when the browser service itself cannot serve requests."""

    def __init__(self, url: str, reason: str) -> None:
        """Initialise the error.

        Args:
            url: The service endpoint that was called.
            reason: What went wrong.
        """
        super().__init__(f"The browser service at {url} is unavailable: {reason}")
        self.url = url
        self.reason = reason


class PageUnreadableError(RuntimeError):
    """Raised when the browser worked but the target page could not be read."""

    def __init__(self, page_url: str, reason: str) -> None:
        """Initialise the error.

        Args:
            page_url: The page that could not be read.
            reason: What went wrong with it.
        """
        super().__init__(f"The page at {page_url} could not be read: {reason}")
        self.page_url = page_url
        self.reason = reason


@dataclass(frozen=True, slots=True, kw_only=True)
class ObservedControl:
    """One control as the rendered page presents it."""

    kind: str
    label: str
    label_source: str
    """Where the label came from: `markup`, `layout`, or empty when none was found.

    This is what decides whether the DOM reading can be trusted for this page. A page whose
    controls are mostly unlabelled has not been understood, however many controls were found,
    and is better read from its screenshots.
    """
    context_path: list[str] = field(default_factory=list)
    name: str = ""
    options: list[str] = field(default_factory=list)
    in_form: bool = True


@dataclass(frozen=True, slots=True, kw_only=True)
class ObservedPage:
    """One rendered page: what it contains, and what it looks like."""

    url: str
    title: str
    text: str
    controls: list[ObservedControl]
    screenshots: list[bytes]

    @property
    def labelled_share(self) -> float:
        """The share of controls that carry a label, from 0 to 1."""
        if not self.controls:
            return 0.0
        return sum(1 for control in self.controls if control.label) / len(self.controls)


class BrowserClient:
    """Renders one page and reports its controls and screenshots.

    One page per call, no crawling: a form spread over several URLs is out of scope, and the
    extractor warns rather than guessing which links are further steps.
    """

    def __init__(
        self,
        *,
        observe_url: str,
        timeout_seconds: int,
        wait_ms: int,
        max_tiles: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Initialise the client.

        Args:
            observe_url: The full `/observe` endpoint of the browser service.
            timeout_seconds: How long a render may take.
            wait_ms: How long the browser waits after load before reading the page, so a
                client-rendered form is observed rendered rather than as its loading shell.
            max_tiles: Cap on the screenshot tiles requested for one page.
            transport: Optional httpx transport, used by tests to avoid real network calls.
        """
        self._observe_url = observe_url
        self._timeout = timeout_seconds
        self._wait_ms = wait_ms
        self._max_tiles = max_tiles
        self._transport = transport

    @property
    def url(self) -> str:
        """The endpoint this client calls."""
        return self._observe_url

    async def observe(self, page_url: str) -> ObservedPage:
        """Render one page and read its controls off it.

        Args:
            page_url: The page to render.

        Returns:
            The controls found, and the page in screenshot tiles.

        Raises:
            BrowserUnavailableError: If the service is unreachable, times out, or errors.
            PageUnreadableError: If the browser answered but the page did not load.
        """
        payload = {
            "url": page_url,
            "waitMs": self._wait_ms,
            "maxTiles": self._max_tiles,
            "screenshots": True,
        }

        try:
            timeout = self._timeout + HTTP_TIMEOUT_HEADROOM_SECONDS
            async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as client:
                response = await client.post(self._observe_url, json=payload)
                if response.status_code == NAVIGATION_FAILURE_STATUS:
                    raise PageUnreadableError(page_url, _error_of(response))
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            logger.warning("browser_error_status", url=self._observe_url, status_code=status)
            raise BrowserUnavailableError(self._observe_url, f"the render returned HTTP {status}") from error
        except httpx.HTTPError as error:
            logger.warning("browser_unreachable", url=self._observe_url, error=str(error))
            raise BrowserUnavailableError(self._observe_url, str(error) or type(error).__name__) from error
        except ValueError as error:
            raise BrowserUnavailableError(self._observe_url, "the response was not valid JSON") from error

        return self._read_response(body, page_url)

    def _read_response(self, body: object, page_url: str) -> ObservedPage:
        """Turn a browser service response into an observed page.

        Raises:
            BrowserUnavailableError: If the response is shaped wrongly.
            PageUnreadableError: If the page answered with an error status.
        """
        if not isinstance(body, dict):
            raise BrowserUnavailableError(self._observe_url, "the response was not a JSON object")

        status = body.get("status")
        if isinstance(status, int) and status and status not in SUCCESSFUL_PAGE_STATUSES:
            logger.warning("rendered_page_error_status", page_url=page_url, status_code=status)
            raise PageUnreadableError(page_url, f"it answered HTTP {status}")

        raw_controls = body.get("controls")
        if not isinstance(raw_controls, list):
            raise BrowserUnavailableError(self._observe_url, "the response contained no controls")

        controls = [_read_control(entry) for entry in raw_controls if isinstance(entry, dict)]
        raw_tiles = body.get("screenshots")
        tiles = raw_tiles if isinstance(raw_tiles, list) else []
        screenshots = [base64.b64decode(tile) for tile in tiles if isinstance(tile, str)]

        page = ObservedPage(
            url=str(body.get("url") or page_url),
            title=str(body.get("title") or ""),
            text=str(body.get("text") or ""),
            controls=controls,
            screenshots=screenshots,
        )

        logger.info(
            "page_observed",
            page_url=page_url,
            controls=len(controls),
            labelled_share=round(page.labelled_share, 2),
            screenshots=len(screenshots),
        )
        return page


def _read_control(entry: dict) -> ObservedControl:
    """Read one control out of the service's JSON."""
    context = entry.get("context_path")
    options = entry.get("options")
    return ObservedControl(
        kind=str(entry.get("kind") or "text"),
        label=str(entry.get("label") or ""),
        label_source=str(entry.get("label_source") or ""),
        context_path=[str(item) for item in context] if isinstance(context, list) else [],
        name=str(entry.get("name") or ""),
        options=[str(item) for item in options] if isinstance(options, list) else [],
        in_form=bool(entry.get("in_form", True)),
    )


def _error_of(response: httpx.Response) -> str:
    """The message a failed render reported, or a plain fallback."""
    try:
        body = response.json()
    except ValueError:
        return "the page did not load"
    return str(body.get("error") or "the page did not load") if isinstance(body, dict) else "the page did not load"

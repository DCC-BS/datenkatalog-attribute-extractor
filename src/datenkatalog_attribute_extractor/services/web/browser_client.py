"""Rendering a form in a browser we control, walking its steps, and reading what it looks like.

The web path used to fetch HTML from Firecrawl and infer everything else from the markup:
which text was a caption, which was a heading, what counted as a control. Self-hosted
Firecrawl returns nothing but HTML — no screenshot, no geometry, no computed styles — so every
one of those questions was answered by guessing from the DOM, and guesses about DOM shape are
guesses about one toolkit's markup. They held for the page they were written against and broke
on the next one.

This client talks to `docker/browser`, a Playwright service in this repository. It renders the
page, walks it step by step where it is a wizard, and returns for each step:

* **tiles** — the step in viewport-sized screenshots, which is what the model reads;
* **controls** — the controls the rendered page presents, each with the caption and heading
  chain measured off the layout, and for every tile which of those controls stand on it.

The controls are not a second reading competing with the picture. They are the page's own
spelling of the labels in it, handed to the model beside the tile they belong to.

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
    """Where the label came from: `markup`, `layout`, or empty when none was found."""
    context_path: list[str] = field(default_factory=list)
    name: str = ""
    options: list[str] = field(default_factory=list)
    in_form: bool = True


@dataclass(frozen=True, slots=True, kw_only=True)
class ObservedTile:
    """One screen of a step: the picture, and the controls standing on it."""

    image: bytes
    controls: list[ObservedControl]


@dataclass(frozen=True, slots=True, kw_only=True)
class ObservedStep:
    """One step of a form: everything the browser saw before it pressed *next*."""

    index: int
    label: str
    """What the page calls this step, where it says so in a standard way. Often empty."""
    title: str
    tiles: list[ObservedTile]
    controls: list[ObservedControl]
    advanced_by: str = ""
    """The wording on the button that led out of this step, empty if none was pressed."""
    blocked_by: list[str] = field(default_factory=list)
    """The controls this step would not accept, where it refused to be left.

    A step that will not advance is the one thing a reviewer has to be able to act on: the
    inventory stops there, and "it would not advance" is only useful with "because of these".
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class ObservedPage:
    """One form as the browser walked it: every step it reached, and why it stopped."""

    url: str
    title: str
    steps: list[ObservedStep]
    stopped_because: str
    """Why the walk ended: `no_next`, `blocked`, `unchanged`, `max_steps`, or `left_site`.

    `blocked` and `unchanged` are the interesting ones: the page refused to advance, which
    normally means a validation the browser could not satisfy. `blocked` is the clearer of the
    two — the next button is right there and greyed out. The inventory is then partial, and the
    last step's `blocked_by` names what it was waiting for.
    """


class BrowserClient:
    """Renders one form, follows its steps, and reports each step in pictures.

    Stepping is a walk, not a crawl: the page's own *next* button is pressed, never a link into
    the wider site, and never anything that reads like a submit.
    """

    def __init__(
        self,
        *,
        observe_url: str,
        timeout_seconds: int,
        wait_ms: int,
        max_tiles: int,
        step_wait_ms: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Initialise the client.

        Args:
            observe_url: The full `/observe` endpoint of the browser service.
            timeout_seconds: How long a render may take.
            wait_ms: How long the browser waits after load before reading the page, so a
                client-rendered form is observed rendered rather than as its loading shell.
            max_tiles: Cap on the screenshot tiles requested per step.
            step_wait_ms: How long the browser waits after pressing *next* before reading the
                step it arrived at.
            transport: Optional httpx transport, used by tests to avoid real network calls.
        """
        self._observe_url = observe_url
        self._timeout = timeout_seconds
        self._wait_ms = wait_ms
        self._max_tiles = max_tiles
        self._step_wait_ms = step_wait_ms
        self._transport = transport

    @property
    def url(self) -> str:
        """The endpoint this client calls."""
        return self._observe_url

    async def observe(self, page_url: str, *, max_steps: int) -> ObservedPage:
        """Render one form and photograph every step of it that can be reached.

        Args:
            page_url: The form to render.
            max_steps: How many steps the browser may walk. One reads the first step only.

        Returns:
            The steps reached, each in screenshot tiles with the controls on them.

        Raises:
            BrowserUnavailableError: If the service is unreachable, times out, or errors.
            PageUnreadableError: If the browser answered but the page did not load.
        """
        payload = {
            "url": page_url,
            "waitMs": self._wait_ms,
            "maxTiles": self._max_tiles,
            "screenshots": True,
            "maxSteps": max_steps,
            "stepWaitMs": self._step_wait_ms,
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

        raw_steps = body.get("steps")
        if not isinstance(raw_steps, list):
            raise BrowserUnavailableError(self._observe_url, "the response contained no steps")

        steps = [_read_step(entry, index) for index, entry in enumerate(raw_steps, start=1) if isinstance(entry, dict)]

        page = ObservedPage(
            url=str(body.get("url") or page_url),
            title=str(body.get("title") or ""),
            steps=steps,
            stopped_because=str(body.get("stopped_because") or ""),
        )

        logger.info(
            "page_observed",
            page_url=page_url,
            steps=len(steps),
            tiles=sum(len(step.tiles) for step in steps),
            controls=sum(len(step.controls) for step in steps),
            stopped_because=page.stopped_because,
        )
        return page


def _read_step(entry: dict, fallback_index: int) -> ObservedStep:
    """Read one step, resolving each tile's control indices into the controls themselves."""
    raw_controls = entry.get("controls")
    controls = (
        [_read_control(item) for item in raw_controls if isinstance(item, dict)]
        if isinstance(raw_controls, list)
        else []
    )

    raw_tiles = entry.get("tiles")
    tiles: list[ObservedTile] = []
    for tile in raw_tiles if isinstance(raw_tiles, list) else []:
        if not isinstance(tile, dict) or not isinstance(tile.get("image"), str):
            continue
        indices = tile.get("control_indices")
        on_tile = (
            [controls[i] for i in indices if isinstance(i, int) and 0 <= i < len(controls)]
            if isinstance(indices, list)
            else []
        )
        tiles.append(ObservedTile(image=base64.b64decode(tile["image"]), controls=on_tile))

    index = entry.get("index")
    return ObservedStep(
        index=index if isinstance(index, int) and index > 0 else fallback_index,
        label=str(entry.get("label") or ""),
        title=str(entry.get("title") or ""),
        tiles=tiles,
        controls=controls,
        advanced_by=str(entry.get("advanced_by") or ""),
        blocked_by=[str(item) for item in blocked] if isinstance(blocked := entry.get("blocked_by"), list) else [],
    )


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

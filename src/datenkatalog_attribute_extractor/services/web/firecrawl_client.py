"""Fetching a form page through a self-hosted Firecrawl instance.

Three things about Firecrawl's API shape drove this client, all of them measured against a
running 2.10.19 instance rather than taken from the documentation:

* **`rawHtml`, never `markdown`.** Markdown has no syntax for a form control, so the
  conversion drops every one of them. On the same page, `rawHtml` returned
  `<label for="fname">First name:</label><input type="text" id="fname" name="fname">`
  while `markdown` returned the bare text `First name:` — label kept, control gone, and with
  it the type, the name and the label-to-control association the whole extraction rests on.

* **A 200 from Firecrawl says nothing about the page.** Scraping a URL that answered 503
  still produced HTTP 200 with `success: true`; the real status was only in
  `data.metadata.statusCode`. Trusting `success` would feed a "503 Service Temporarily
  Unavailable" error page to the model and report an empty inventory as a clean run, which
  is exactly the failure the PDF path was redesigned to eliminate.

* **Partial results are announced in `data.warning`.** Firecrawl reports features the
  chosen engine could not honour there rather than failing, so it is surfaced to the
  reviewer instead of being dropped.

Screenshots are deliberately not requested. Self-hosted Firecrawl cannot produce them: its
playwright engine adapter neither asks the browser service for one nor accepts one back —
the response schema admits only `content`, `pageStatusCode`, `pageError` and `contentType`.
Screenshots and `actions` both live in Fire Engine, which is cloud-only, as SELF_HOST.md
states. Verified against both the official image and a direct probe of the browser service.
"""

from dataclasses import dataclass

import httpx
from dcc_backend_common.logger import get_logger

logger = get_logger(__name__)

# Firecrawl reports the page's own status here; its HTTP status only describes the scrape.
SUCCESSFUL_PAGE_STATUSES = range(200, 300)


class FirecrawlUnavailableError(RuntimeError):
    """Raised when Firecrawl itself cannot serve requests."""

    def __init__(self, url: str, reason: str) -> None:
        """Initialise the error.

        Args:
            url: The Firecrawl endpoint that was called.
            reason: What went wrong.
        """
        super().__init__(f"Firecrawl at {url} is unavailable: {reason}")
        self.url = url
        self.reason = reason


class PageUnreadableError(RuntimeError):
    """Raised when Firecrawl worked but the target page could not be read."""

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
class ScrapedPage:
    """One scraped page, as far as extraction is concerned."""

    url: str
    html: str
    title: str
    warnings: list[str]


class FirecrawlClient:
    """Scrapes a single page and hands back its raw HTML.

    One page per call, no crawling: a multi-step form spread over several URLs is out of
    scope, and the extractor warns rather than guessing which links are further steps.
    """

    def __init__(
        self,
        *,
        scrape_url: str,
        timeout_seconds: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Initialise the client.

        Args:
            scrape_url: The full `/v2/scrape` endpoint.
            timeout_seconds: How long to wait for a scrape, which renders in a real browser.
            transport: Optional httpx transport, used by tests to avoid real network calls.

        Note:
            No API key is configured. Self-hosted Firecrawl runs with
            `USE_DB_AUTHENTICATION=false` and accepts unauthenticated requests.
        """
        self._scrape_url = scrape_url
        self._timeout = timeout_seconds
        self._transport = transport

    @property
    def url(self) -> str:
        """The endpoint this client calls."""
        return self._scrape_url

    async def scrape(self, page_url: str) -> ScrapedPage:
        """Fetch one page as raw HTML.

        Args:
            page_url: The page to scrape.

        Returns:
            The page's HTML together with any warnings Firecrawl raised.

        Raises:
            FirecrawlUnavailableError: If Firecrawl is unreachable, times out, or errors.
            PageUnreadableError: If Firecrawl answered but the page did not.
        """
        payload = {
            "url": page_url,
            "formats": ["rawHtml"],
            # The page furniture around a form — nav, cookie banners, footers — is dropped
            # later by the control listing, and onlyMainContent has been seen to take real
            # fields with it, so the full document is requested and reduced under our rules.
            "onlyMainContent": False,
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                response = await client.post(self._scrape_url, json=payload)
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            logger.warning("firecrawl_error_status", url=self._scrape_url, status_code=status)
            raise FirecrawlUnavailableError(self._scrape_url, f"scrape returned HTTP {status}") from error
        except httpx.HTTPError as error:
            logger.warning("firecrawl_unreachable", url=self._scrape_url, error=str(error))
            raise FirecrawlUnavailableError(self._scrape_url, str(error) or type(error).__name__) from error
        except ValueError as error:
            raise FirecrawlUnavailableError(self._scrape_url, "the response was not valid JSON") from error

        return self._read_response(body, page_url)

    def _read_response(self, body: object, page_url: str) -> ScrapedPage:
        """Turn a Firecrawl response body into a scraped page.

        Raises:
            FirecrawlUnavailableError: If the response is shaped wrongly or reports failure.
            PageUnreadableError: If the page answered with an error status or no HTML.
        """
        if not isinstance(body, dict):
            raise FirecrawlUnavailableError(self._scrape_url, "the response was not a JSON object")

        if not body.get("success", False):
            reason = str(body.get("error") or "the scrape reported failure")
            raise FirecrawlUnavailableError(self._scrape_url, reason)

        data = body.get("data")
        if not isinstance(data, dict):
            raise FirecrawlUnavailableError(self._scrape_url, "the response contained no data")

        raw_metadata = data.get("metadata")
        metadata: dict = raw_metadata if isinstance(raw_metadata, dict) else {}
        status = metadata.get("statusCode")
        if isinstance(status, int) and status not in SUCCESSFUL_PAGE_STATUSES:
            detail = metadata.get("error") or "no detail"
            logger.warning("scraped_page_error_status", page_url=page_url, status_code=status)
            raise PageUnreadableError(page_url, f"it answered HTTP {status} ({detail})")

        html = data.get("rawHtml")
        if not isinstance(html, str) or not html.strip():
            raise PageUnreadableError(page_url, "no HTML was returned")

        warnings: list[str] = []
        warning = data.get("warning")
        if isinstance(warning, str) and warning:
            logger.info("firecrawl_partial_scrape", page_url=page_url, warning=warning)
            warnings.append(f"Firecrawl reported a partial scrape: {warning}")

        logger.info("page_scraped", page_url=page_url, html_bytes=len(html), status_code=status)
        return ScrapedPage(
            url=str(metadata.get("sourceURL") or page_url),
            html=html,
            title=str(metadata.get("title") or ""),
            warnings=warnings,
        )

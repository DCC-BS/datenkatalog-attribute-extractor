"""Availability checks for the LLM API.

Extraction is expensive: a document is rasterised page by page and every page costs a model
call. If the LLM is unreachable or misconfigured, none of that work can succeed, so the run
must abort immediately rather than grind through every page collecting identical failures.
"""

import httpx
import openai
from dcc_backend_common.logger import get_logger
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError

logger = get_logger(__name__)

# Failures that no amount of retrying or moving to the next page will fix:
# the service is down, hanging, broken, or we are pointed at it wrongly.
# APITimeoutError is a subclass of APIConnectionError, so both are covered.
FATAL_LLM_ERRORS: tuple[type[BaseException], ...] = (
    openai.APIConnectionError,
    openai.InternalServerError,
    openai.AuthenticationError,
    openai.PermissionDeniedError,
    openai.NotFoundError,
    httpx.TransportError,
)

# HTTP statuses that mean the service or our configuration is wrong, not this page.
# 400/413/422 are excluded: those describe the request we just sent for one page.
# 429 is excluded too, being transient and already retried by the agent's transport.
FATAL_HTTP_STATUSES = frozenset({401, 403, 404})

# How far to follow `__cause__` and exception-group members when classifying.
MAX_CAUSE_DEPTH = 10


class LlmUnavailableError(RuntimeError):
    """Raised when the LLM API cannot serve requests at all."""

    def __init__(self, url: str, reason: str) -> None:
        """Initialise the error.

        Args:
            url: The endpoint that was checked or called.
            reason: What went wrong.
        """
        super().__init__(f"The LLM API at {url} is unavailable: {reason}")
        self.url = url
        self.reason = reason


def _related_errors(error: BaseException, depth: int = 0) -> list[BaseException]:
    """Flatten an exception together with its causes and any grouped members.

    pydantic-ai reports provider failures as `ModelAPIError` and keeps the underlying
    `openai`/`httpx` exception only in `__cause__`, so classification has to look through the
    chain rather than at the outermost type alone. `__context__` is deliberately *not*
    followed: it would pick up unrelated ambient exceptions and misclassify page-local
    failures as fatal.

    Args:
        error: The exception to flatten.
        depth: Current recursion depth.

    Returns:
        The exception itself plus everything reachable through `__cause__` and group members.
    """
    if depth >= MAX_CAUSE_DEPTH:
        return [error]

    related = [error]
    if error.__cause__ is not None:
        related.extend(_related_errors(error.__cause__, depth + 1))
    if isinstance(error, BaseExceptionGroup):
        for member in error.exceptions:
            related.extend(_related_errors(member, depth + 1))
    return related


def is_fatal_llm_error(error: BaseException) -> bool:
    """Report whether an error means the LLM is unusable for the whole run.

    A dead server, a hanging server, bad credentials or a wrong model name fail identically
    on every page, so those abort the run. Problems that belong to one page — an unparsable
    response, an image the model rejects, a rate limit — do not.

    Args:
        error: The exception raised while calling the model.

    Returns:
        True if the run should be aborted rather than continued on the next page.
    """
    for candidate in _related_errors(error):
        if isinstance(candidate, ModelHTTPError):
            if candidate.status_code in FATAL_HTTP_STATUSES or candidate.status_code >= 500:
                return True
            continue
        # A ModelAPIError that is not an HTTP error is a transport failure: the request
        # never got a response.
        if isinstance(candidate, ModelAPIError):
            return True
        if isinstance(candidate, FATAL_LLM_ERRORS):
            return True
    return False


class LlmHealthProbe:
    """Checks that the LLM API is reachable before an extraction run starts.

    Uses its own short timeout, independent of `llm_timeout`, so that a hanging server is
    detected in seconds instead of minutes.
    """

    def __init__(
        self,
        *,
        health_check_url: str,
        models_url: str,
        model_name: str,
        api_key: str,
        timeout_seconds: int,
        min_context_tokens: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Initialise the probe.

        Args:
            health_check_url: The LLM service's health endpoint.
            models_url: The OpenAI-compatible `/models` endpoint.
            model_name: The model this service expects to call.
            api_key: Sent as a bearer token when non-empty.
            timeout_seconds: How long to wait before declaring the service unavailable.
            min_context_tokens: Refuse to start a run if the served context is smaller than
                this, since prompt plus image plus answer cannot fit.
            transport: Optional httpx transport, used by tests to avoid real network calls.
        """
        self._url = health_check_url
        self._models_url = models_url
        self._model_name = model_name
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._min_context_tokens = min_context_tokens
        self._transport = transport
        self._served_context_tokens: int | None = None

    @property
    def url(self) -> str:
        """The endpoint being probed."""
        return self._url

    @property
    def served_context_tokens(self) -> int | None:
        """The `max_model_len` the provider last reported, or None if it never said.

        Read off the same `/models` call that validates the model, so callers can size a
        prompt to the context actually being served rather than to a number in a config file.
        The two differ by more than an order of magnitude between environments here —
        16384 on the development box against 250000 in production — which is the difference
        between sending a form page in one call and having to split it.
        """
        return self._served_context_tokens

    @property
    def _headers(self) -> dict[str, str]:
        """Authorisation headers, if an API key is configured."""
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    async def ensure_available(self) -> None:
        """Verify the LLM API is reachable and able to serve this workload.

        Raises:
            LlmUnavailableError: If the endpoint is unreachable, times out, reports an error
                status, does not serve the configured model, or serves it with a context too
                small for a form page.
        """
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            await self._check_health(client)
            await self._check_model(client)

    async def _check_health(self, client: httpx.AsyncClient) -> None:
        """Confirm the service answers its health endpoint."""
        try:
            response = await client.get(self._url, headers=self._headers)
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            logger.warning("llm_unhealthy", url=self._url, status_code=error.response.status_code)
            raise LlmUnavailableError(self._url, f"health check returned HTTP {error.response.status_code}") from error
        except httpx.HTTPError as error:
            logger.warning("llm_unreachable", url=self._url, error=str(error))
            raise LlmUnavailableError(self._url, str(error) or type(error).__name__) from error

    async def _check_model(self, client: httpx.AsyncClient) -> None:
        """Confirm the configured model is served with a usable context length.

        A served context that cannot hold the prompt, the page image and the answer produces
        truncated nonsense rather than an error, so it is rejected up front. This is easy to
        hit with `--max-model-len auto`, which silently shrinks to whatever KV cache is left.
        """
        try:
            response = await client.get(self._models_url, headers=self._headers)
            response.raise_for_status()
            entries = response.json().get("data", [])
        except (httpx.HTTPError, ValueError) as error:
            # Not fatal on its own: the health endpoint already passed, and a provider that
            # does not expose /models should not block extraction.
            logger.warning("llm_model_list_unavailable", url=self._models_url, error=str(error))
            return

        served = {entry.get("id"): entry for entry in entries if isinstance(entry, dict)}
        if self._model_name not in served:
            available = ", ".join(sorted(name for name in served if name)) or "none"
            raise LlmUnavailableError(
                self._models_url,
                f"model '{self._model_name}' is not served (available: {available})",
            )

        context_length = served[self._model_name].get("max_model_len")
        if isinstance(context_length, int):
            self._served_context_tokens = context_length
        if isinstance(context_length, int) and context_length < self._min_context_tokens:
            logger.error(
                "llm_context_too_small",
                model=self._model_name,
                max_model_len=context_length,
                required=self._min_context_tokens,
            )
            raise LlmUnavailableError(
                self._models_url,
                f"model '{self._model_name}' is served with max_model_len={context_length}, "
                f"below the {self._min_context_tokens} tokens needed for prompt, page image and answer. "
                "Set --max-model-len explicitly on vLLM instead of leaving it on 'auto'",
            )

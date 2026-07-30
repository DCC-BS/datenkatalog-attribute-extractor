"""Tests for LLM availability checks and fatal-error classification."""

from collections.abc import Callable

import httpx
import openai
import pytest
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UnexpectedModelBehavior

from datenkatalog_attribute_extractor.services.llm_health import (
    LlmHealthProbe,
    LlmUnavailableError,
    is_fatal_llm_error,
)

HEALTH_URL = "http://llm:8000/health"
MODELS_URL = "http://llm:8000/v1/models"
MODEL_NAME = "Gemma/Gemma-4-31B"


def make_probe(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    api_key: str = "",
    timeout_seconds: int = 5,
    min_context_tokens: int = 8192,
) -> LlmHealthProbe:
    """Build a real probe whose HTTP calls are served by `handler` instead of the network."""
    return LlmHealthProbe(
        health_check_url=HEALTH_URL,
        models_url=MODELS_URL,
        model_name=MODEL_NAME,
        api_key=api_key,
        timeout_seconds=timeout_seconds,
        min_context_tokens=min_context_tokens,
        transport=httpx.MockTransport(handler),
    )


def serve(
    *,
    health_status: int = 200,
    models: list[dict] | None = None,
    models_status: int = 200,
) -> Callable[[httpx.Request], httpx.Response]:
    """A handler answering both the health and the models endpoint."""
    entries = models if models is not None else [{"id": MODEL_NAME, "max_model_len": 16384}]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(models_status, json={"data": entries})
        return httpx.Response(health_status)

    return handler


def respond_with(status_code: int) -> Callable[[httpx.Request], httpx.Response]:
    """A handler returning a fixed status code for the health endpoint."""
    return serve(health_status=status_code)


def raise_error(error: Exception) -> Callable[[httpx.Request], httpx.Response]:
    """A handler that fails the request."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    return handler


async def test_ensure_available_with_a_healthy_service_returns_normally() -> None:
    await make_probe(respond_with(200)).ensure_available()


async def test_ensure_available_with_an_error_status_raises() -> None:
    with pytest.raises(LlmUnavailableError, match="HTTP 503"):
        await make_probe(respond_with(503)).ensure_available()


async def test_ensure_available_when_the_service_refuses_the_connection_raises() -> None:
    probe = make_probe(raise_error(httpx.ConnectError("Connection refused")))

    with pytest.raises(LlmUnavailableError, match="unavailable"):
        await probe.ensure_available()


async def test_ensure_available_when_the_service_hangs_raises() -> None:
    probe = make_probe(raise_error(httpx.ReadTimeout("timed out")))

    with pytest.raises(LlmUnavailableError):
        await probe.ensure_available()


async def test_ensure_available_names_the_endpoint_in_the_error() -> None:
    probe = make_probe(raise_error(httpx.ConnectError("Connection refused")))

    with pytest.raises(LlmUnavailableError) as raised:
        await probe.ensure_available()

    assert raised.value.url == HEALTH_URL
    assert HEALTH_URL in str(raised.value)


async def test_ensure_available_sends_the_api_key_as_a_bearer_token() -> None:
    seen: list[str | None] = []
    inner = serve()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization"))
        return inner(request)

    await make_probe(handler, api_key="secret").ensure_available()

    assert seen == ["Bearer secret", "Bearer secret"]


async def test_ensure_available_omits_the_header_when_no_api_key_is_set() -> None:
    seen: list[str | None] = []
    inner = serve()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization"))
        return inner(request)

    await make_probe(handler, api_key="").ensure_available()

    assert seen == [None, None]


async def test_ensure_available_rejects_a_context_too_small_for_a_form_page() -> None:
    """The exact failure that made the integration suite red: max-model-len auto shrank to 2544."""
    handler = serve(models=[{"id": MODEL_NAME, "max_model_len": 2544}])

    with pytest.raises(LlmUnavailableError, match="max_model_len=2544") as raised:
        await make_probe(handler, min_context_tokens=8192).ensure_available()

    assert "--max-model-len" in str(raised.value)


async def test_ensure_available_accepts_a_sufficient_context() -> None:
    handler = serve(models=[{"id": MODEL_NAME, "max_model_len": 16384}])

    await make_probe(handler, min_context_tokens=8192).ensure_available()


async def test_ensure_available_rejects_a_model_the_service_does_not_serve() -> None:
    handler = serve(models=[{"id": "some/other-model", "max_model_len": 32768}])

    with pytest.raises(LlmUnavailableError, match="is not served") as raised:
        await make_probe(handler).ensure_available()

    assert "some/other-model" in str(raised.value)


async def test_ensure_available_tolerates_a_provider_without_a_models_endpoint() -> None:
    """The health endpoint already passed; a missing /models must not block extraction."""
    handler = serve(models_status=404)

    await make_probe(handler).ensure_available()


async def test_ensure_available_tolerates_a_models_entry_without_a_context_length() -> None:
    handler = serve(models=[{"id": MODEL_NAME}])

    await make_probe(handler).ensure_available()


@pytest.mark.parametrize(
    "error",
    [
        openai.APIConnectionError(request=httpx.Request("POST", "http://llm/v1")),
        openai.APITimeoutError(request=httpx.Request("POST", "http://llm/v1")),
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
    ],
)
def test_is_fatal_llm_error_treats_connection_problems_as_fatal(error: Exception) -> None:
    assert is_fatal_llm_error(error)


@pytest.mark.parametrize(
    ("status_code", "error_class"),
    [
        (500, openai.InternalServerError),
        (401, openai.AuthenticationError),
        (403, openai.PermissionDeniedError),
        (404, openai.NotFoundError),
    ],
)
def test_is_fatal_llm_error_treats_misconfiguration_and_server_faults_as_fatal(
    status_code: int, error_class: type[openai.APIStatusError]
) -> None:
    response = httpx.Response(status_code, request=httpx.Request("POST", "http://llm/v1"))

    assert is_fatal_llm_error(error_class("boom", response=response, body=None))


@pytest.mark.parametrize(
    "error",
    [
        ValueError("could not parse the model output"),
        RuntimeError("something page-specific"),
        UnexpectedModelBehavior("the model returned no output"),
    ],
)
def test_is_fatal_llm_error_leaves_page_local_failures_recoverable(error: Exception) -> None:
    assert not is_fatal_llm_error(error)


def test_is_fatal_llm_error_treats_rate_limiting_as_recoverable() -> None:
    """Rate limits are transient and already retried by the agent's HTTP transport."""
    response = httpx.Response(429, request=httpx.Request("POST", "http://llm/v1"))

    assert not is_fatal_llm_error(openai.RateLimitError("slow down", response=response, body=None))


def test_is_fatal_llm_error_sees_through_pydantic_ai_wrapping() -> None:
    """pydantic-ai reports provider failures as ModelAPIError, keeping the real cause in __cause__."""
    cause = openai.APIConnectionError(request=httpx.Request("POST", "http://llm/v1"))
    wrapped = ModelAPIError(model_name="gemma", message="Connection error.")
    wrapped.__cause__ = cause

    assert is_fatal_llm_error(wrapped)


def test_is_fatal_llm_error_treats_a_bare_transport_model_api_error_as_fatal() -> None:
    assert is_fatal_llm_error(ModelAPIError(model_name="gemma", message="Connection error."))


@pytest.mark.parametrize("status_code", [401, 403, 404, 500, 502, 503])
def test_is_fatal_llm_error_treats_server_and_configuration_statuses_as_fatal(status_code: int) -> None:
    error = ModelHTTPError(status_code=status_code, model_name="gemma", body="boom")

    assert is_fatal_llm_error(error)


@pytest.mark.parametrize("status_code", [400, 413, 422, 429])
def test_is_fatal_llm_error_treats_request_specific_statuses_as_page_local(status_code: int) -> None:
    error = ModelHTTPError(status_code=status_code, model_name="gemma", body="that page")

    assert not is_fatal_llm_error(error)


def test_is_fatal_llm_error_finds_a_fatal_cause_inside_an_exception_group() -> None:
    group = ExceptionGroup(
        "retries failed",
        [ValueError("unrelated"), httpx.ConnectError("refused")],
    )

    assert is_fatal_llm_error(group)


def test_is_fatal_llm_error_does_not_follow_unrelated_context() -> None:
    """A page-local error raised while another was being handled must stay page-local."""
    page_error = ValueError("could not parse the model output")
    page_error.__context__ = httpx.ConnectError("an earlier, unrelated failure")

    assert not is_fatal_llm_error(page_error)


def test_is_fatal_llm_error_survives_a_self_referential_cause_chain() -> None:
    error = ModelAPIError(model_name="gemma", message="boom")
    error.__cause__ = error

    assert is_fatal_llm_error(error)

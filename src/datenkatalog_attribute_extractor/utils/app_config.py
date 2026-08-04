"""Application configuration loaded from environment variables."""

import os
from typing import override

from dcc_backend_common.config import get_env_or_throw, log_secret
from dcc_backend_common.config.app_config import LlmConfig
from pydantic import Field

BYTES_PER_MEGABYTE = 1024 * 1024


class AppConfig(LlmConfig):
    """Configuration for the form field extraction service.

    Inherits `llm_model`, `llm_url`, `llm_api_key`, `llm_timeout` and `llm_max_retries`
    from `LlmConfig`.
    """

    llm_health_check_url: str = Field(description="Health endpoint of the LLM service, used by the readiness probe")
    llm_health_timeout: int = Field(
        default=5,
        ge=1,
        description="Seconds to wait for the LLM health check before failing an extraction fast",
    )
    llm_min_context_tokens: int = Field(
        default=8192,
        ge=1024,
        description="Refuse to extract if the served max_model_len is below this; prompt, page image and answer will not fit",
    )
    llm_max_concurrency: int = Field(
        default=1,
        ge=1,
        description="Pages sent to the LLM at once. Keep aligned with the vLLM --max-num-seqs setting",
    )
    pdf_render_dpi: int = Field(
        default=200,
        ge=72,
        le=600,
        description="Resolution used to rasterise PDF pages before sending them to the model",
    )
    max_pages: int = Field(default=30, ge=1, description="Maximum number of pages processed per document")
    max_upload_mb: int = Field(default=25, ge=1, description="Maximum accepted upload size in megabytes")
    firecrawl_api_url: str = Field(
        default="http://localhost:3002",
        description="Base URL of the Firecrawl instance used to scrape web forms",
    )
    firecrawl_timeout: int = Field(
        default=120,
        ge=1,
        description="Seconds to wait for a Firecrawl scrape, which renders the page in a real browser",
    )
    max_web_units: int = Field(
        default=30,
        ge=1,
        description="Maximum HTML chunks processed per web page, mirroring max_pages",
    )
    client_url: str = Field(default="http://localhost:8501", description="Origin allowed by CORS")
    backend_url: str = Field(
        default="http://localhost:8000",
        description="Base URL the Streamlit UI uses to reach this API",
    )

    @property
    def max_upload_bytes(self) -> int:
        """The upload size limit in bytes."""
        return self.max_upload_mb * BYTES_PER_MEGABYTE

    @property
    def llm_models_url(self) -> str:
        """The OpenAI-compatible `/models` endpoint of the LLM service."""
        return f"{self.llm_url.rstrip('/')}/models"

    @property
    def firecrawl_scrape_url(self) -> str:
        """The Firecrawl scrape endpoint.

        Firecrawl 2.x serves the v2 API; the v1 path is still routed but deprecated upstream.
        """
        return f"{self.firecrawl_api_url.rstrip('/')}/v2/scrape"

    @classmethod
    @override
    def from_env(cls) -> "AppConfig":
        """Load configuration from environment variables.

        Returns:
            The populated configuration.

        Raises:
            AppConfigError: If a required environment variable is missing.
        """
        llm_url = get_env_or_throw("LLM_URL")
        return cls(
            llm_model=get_env_or_throw("LLM_MODEL"),
            llm_url=llm_url,
            llm_api_key=os.getenv("LLM_API_KEY", "not-needed"),
            llm_timeout=int(os.getenv("LLM_TIMEOUT", "300")),
            llm_max_retries=int(os.getenv("LLM_MAX_RETRIES", "2")),
            llm_health_check_url=os.getenv("LLM_HEALTH_CHECK_URL", f"{llm_url.rstrip('/').removesuffix('/v1')}/health"),
            llm_health_timeout=int(os.getenv("LLM_HEALTH_TIMEOUT", "5")),
            llm_min_context_tokens=int(os.getenv("LLM_MIN_CONTEXT_TOKENS", "8192")),
            llm_max_concurrency=int(os.getenv("LLM_MAX_CONCURRENCY", "1")),
            pdf_render_dpi=int(os.getenv("PDF_RENDER_DPI", "200")),
            max_pages=int(os.getenv("MAX_PAGES", "30")),
            max_upload_mb=int(os.getenv("MAX_UPLOAD_MB", "25")),
            client_url=os.getenv("CLIENT_URL", "http://localhost:8501"),
            backend_url=os.getenv("BACKEND_URL", "http://localhost:8000"),
            firecrawl_api_url=os.getenv("FIRECRAWL_API_URL", "http://localhost:3002"),
            firecrawl_timeout=int(os.getenv("FIRECRAWL_TIMEOUT", "120")),
            max_web_units=int(os.getenv("MAX_WEB_UNITS", "30")),
        )

    @override
    def __str__(self) -> str:
        """Return a string representation with secrets masked."""
        return (
            "AppConfig("
            f"llm_model={self.llm_model}, "
            f"llm_url={self.llm_url}, "
            f"llm_api_key={log_secret(self.llm_api_key)}, "
            f"llm_timeout={self.llm_timeout}, "
            f"llm_max_retries={self.llm_max_retries}, "
            f"llm_health_check_url={self.llm_health_check_url}, "
            f"llm_health_timeout={self.llm_health_timeout}, "
            f"llm_min_context_tokens={self.llm_min_context_tokens}, "
            f"llm_max_concurrency={self.llm_max_concurrency}, "
            f"pdf_render_dpi={self.pdf_render_dpi}, "
            f"max_pages={self.max_pages}, "
            f"max_upload_mb={self.max_upload_mb}, "
            f"client_url={self.client_url}, "
            f"backend_url={self.backend_url}, "
            f"firecrawl_api_url={self.firecrawl_api_url}, "
            f"firecrawl_timeout={self.firecrawl_timeout}, "
            f"max_web_units={self.max_web_units})"
        )

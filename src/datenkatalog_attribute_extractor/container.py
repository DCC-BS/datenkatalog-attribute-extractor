"""Dependency injection container."""

from dependency_injector import containers, providers

from datenkatalog_attribute_extractor.services.agents.form_field_agent import FormFieldExtractionAgent
from datenkatalog_attribute_extractor.services.agents.web_field_agent import WebFieldExtractionAgent
from datenkatalog_attribute_extractor.services.extraction_service import ExtractionService
from datenkatalog_attribute_extractor.services.extractors.pdf_extractor import PdfFieldExtractor
from datenkatalog_attribute_extractor.services.extractors.protocol import ExtractorRegistry
from datenkatalog_attribute_extractor.services.extractors.web_extractor import WebFieldExtractor
from datenkatalog_attribute_extractor.services.llm_health import LlmHealthProbe
from datenkatalog_attribute_extractor.services.web.firecrawl_client import FirecrawlClient
from datenkatalog_attribute_extractor.utils.app_config import AppConfig


class Container(containers.DeclarativeContainer):
    """Wires configuration, extractors and services together.

    Registering a new source kind means adding its extractor here and appending it to
    `extractor_registry`; nothing else in the application changes.
    """

    # Singleton rather than Object: the config is read on first use, not at import time, so
    # importing the container (in tests, tooling, or the UI) does not require a full environment.
    config: providers.Singleton[AppConfig] = providers.Singleton(AppConfig.from_env)

    form_field_agent: providers.Singleton[FormFieldExtractionAgent] = providers.Singleton(
        FormFieldExtractionAgent,
        config=config,
    )

    llm_health_probe: providers.Singleton[LlmHealthProbe] = providers.Singleton(
        LlmHealthProbe,
        health_check_url=config.provided.llm_health_check_url,
        models_url=config.provided.llm_models_url,
        model_name=config.provided.llm_model,
        api_key=config.provided.llm_api_key,
        timeout_seconds=config.provided.llm_health_timeout,
        min_context_tokens=config.provided.llm_min_context_tokens,
    )

    pdf_extractor: providers.Singleton[PdfFieldExtractor] = providers.Singleton(
        PdfFieldExtractor,
        agent=form_field_agent,
        health_probe=llm_health_probe,
        render_dpi=config.provided.pdf_render_dpi,
        max_pages=config.provided.max_pages,
        max_concurrency=config.provided.llm_max_concurrency,
    )

    firecrawl_client: providers.Singleton[FirecrawlClient] = providers.Singleton(
        FirecrawlClient,
        scrape_url=config.provided.firecrawl_scrape_url,
        timeout_seconds=config.provided.firecrawl_timeout,
        wait_ms=config.provided.firecrawl_wait_ms,
    )

    web_field_agent: providers.Singleton[WebFieldExtractionAgent] = providers.Singleton(
        WebFieldExtractionAgent,
        config=config,
    )

    web_extractor: providers.Singleton[WebFieldExtractor] = providers.Singleton(
        WebFieldExtractor,
        agent=web_field_agent,
        client=firecrawl_client,
        health_probe=llm_health_probe,
        max_units=config.provided.max_web_units,
    )

    extractor_registry: providers.Singleton[ExtractorRegistry] = providers.Singleton(
        ExtractorRegistry,
        extractors=providers.List(pdf_extractor, web_extractor),
    )

    extraction_service: providers.Singleton[ExtractionService] = providers.Singleton(
        ExtractionService,
        registry=extractor_registry,
        max_upload_bytes=config.provided.max_upload_bytes,
    )

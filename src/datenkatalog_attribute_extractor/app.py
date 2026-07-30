"""FastAPI application factory."""

from dcc_backend_common.fastapi_error_handling import inject_api_error_handler
from dcc_backend_common.fastapi_health_probes import health_probe_router
from dcc_backend_common.fastapi_health_probes.router import ServiceDependency
from dcc_backend_common.fastapi_logging_middleware import add_logging_middleware
from dcc_backend_common.logger import get_logger, init_logger
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from datenkatalog_attribute_extractor.container import Container
from datenkatalog_attribute_extractor.routers import extraction


def create_app() -> FastAPI:
    """Build the FastAPI application.

    Returns:
        The configured application.
    """
    init_logger()

    logger = get_logger("app")
    logger.info("Starting form field extraction API")

    container = Container()
    container.wire(modules=[extraction])
    container.check_dependencies()

    config = container.config()
    logger.info("Configuration loaded", config=str(config))

    service_dependencies: list[ServiceDependency] = [
        {
            "name": "llm",
            "health_check_url": config.llm_health_check_url,
            "api_key": config.llm_api_key,
        }
    ]

    app = FastAPI(
        title="Datenkatalog Attribute Extractor",
        description="Extracts uniquely named form fields from questionnaires using a local vision LLM.",
        version="0.1.0",
    )

    app.include_router(health_probe_router(service_dependencies))
    inject_api_error_handler(app)
    add_logging_middleware(app)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[config.client_url],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(extraction.create_router())

    logger.info("API setup complete")
    return app


app = create_app()

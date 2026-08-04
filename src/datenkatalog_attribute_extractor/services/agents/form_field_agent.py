"""The vision agent that reads form fields off a rendered page."""

from types import NoneType
from typing import override

from dcc_backend_common.config.app_config import LlmConfig
from dcc_backend_common.llm_agent import BaseAgent
from pydantic_ai import Agent, BinaryContent, NativeOutput
from pydantic_ai.models import Model

from datenkatalog_attribute_extractor.models.field import PageExtraction
from datenkatalog_attribute_extractor.services.agents.instructions import load_instructions

PROMPT_FILE = "pdf_form_fields.md"

TEMPERATURE = 0.0


class FormFieldExtractionAgent(BaseAgent[None, PageExtraction]):
    """Extracts the input fields of a single form page from its rendered image.

    The agent deliberately does not name fields. It reports labels verbatim together with
    their enclosing headings; unique names are derived afterwards by
    `services.naming.ensure_unique_names`, so that uniqueness is guaranteed rather than
    left to the model.
    """

    def __init__(self, config: LlmConfig) -> None:
        """Initialise the agent.

        Args:
            config: LLM connection settings.
        """
        super().__init__(config, deps_type=None, output_type=PageExtraction)

    @override
    def create_agent(self, model: Model) -> Agent[None, PageExtraction]:
        """Build the underlying pydantic-ai agent."""
        # NativeOutput selects vLLM's guided JSON decoding (`response_format: json_schema`).
        # A bare `output_type=PageExtraction` would instead make pydantic-ai request the
        # output as a tool call, which vLLM rejects outright unless it was started with
        # --tool-call-parser and a tool chat template:
        #   400 tool_choice="required" requires --tool-call-parser to be set
        # Guided JSON needs neither, which is why it is preferred here.
        #
        # deps_type is NoneType, not None: pydantic-ai wants the *type* of the dependencies.
        # The constructor is parameterised explicitly because pydantic-ai's overloads type
        # output_type loosely, so the output type cannot be inferred from the argument.
        return Agent[None, PageExtraction](
            model=model,
            deps_type=NoneType,
            output_type=NativeOutput(PageExtraction),
            instructions=load_instructions(PROMPT_FILE),
        )

    async def extract_page(self, png_bytes: bytes) -> PageExtraction:
        """Read the form fields visible on one rendered page.

        Args:
            png_bytes: The page rendered as a PNG image.

        Returns:
            The fields found on that page, without names.
        """
        # max_tokens is deliberately not set: vLLM then allows the whole remaining context
        # for the answer. Pinning a number here fails outright whenever it exceeds the
        # server's max_model_len, which is easy to hit when that is auto-sized.
        return await self.run(
            [
                "Extract the input fields of this form page.",
                BinaryContent(data=png_bytes, media_type="image/png"),
            ],
            model_settings={"temperature": TEMPERATURE},
        )

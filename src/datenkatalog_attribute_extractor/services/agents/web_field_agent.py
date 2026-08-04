"""The agent that reads form fields off a listing of a page's HTML controls."""

from types import NoneType
from typing import override

from dcc_backend_common.config.app_config import LlmConfig
from dcc_backend_common.llm_agent import BaseAgent
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.models import Model

from datenkatalog_attribute_extractor.models.field import PageExtraction
from datenkatalog_attribute_extractor.services.agents.instructions import load_instructions

PROMPT_FILE = "web_form_fields.md"

TEMPERATURE = 0.0


class WebFieldExtractionAgent(BaseAgent[None, PageExtraction]):
    """Extracts the input fields of a web form from its reduced control listing.

    The counterpart of `FormFieldExtractionAgent`, which reads a rendered page image. Both
    return the same `PageExtraction` and neither names fields; unique names are derived
    afterwards by `services.naming.ensure_unique_names`.

    This one is text-only. A web page is not naturally an image, and its DOM states the
    structure — `label for`, `fieldset`/`legend`, heading nesting — that a picture would leave
    the model to infer from layout.
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
        # NativeOutput for the same reason as the vision agent: a bare output_type makes
        # pydantic-ai request the result as a tool call, which vLLM rejects unless started
        # with --tool-call-parser. Guided JSON decoding needs no extra server flags.
        return Agent[None, PageExtraction](
            model=model,
            deps_type=NoneType,
            output_type=NativeOutput(PageExtraction),
            instructions=load_instructions(PROMPT_FILE),
        )

    async def extract_listing(self, listing: str) -> PageExtraction:
        """Read the form fields described by one control listing.

        Args:
            listing: The reduced controls of a page, or of one chunk of it.

        Returns:
            The fields found, without names.
        """
        # max_tokens is deliberately unset, as in the vision agent: pinning a number fails
        # whenever it exceeds the server's max_model_len.
        return await self.run(
            [
                "Extract the input fields of this form.",
                listing,
            ],
            model_settings={"temperature": TEMPERATURE},
        )

"""The agent that reads form fields off a screenshot of a web form."""

from types import NoneType
from typing import override

from dcc_backend_common.config.app_config import LlmConfig
from dcc_backend_common.llm_agent import BaseAgent
from pydantic_ai import Agent, BinaryContent, NativeOutput
from pydantic_ai.models import Model

from datenkatalog_attribute_extractor.models.field import PageExtraction
from datenkatalog_attribute_extractor.services.agents.instructions import load_instructions

PROMPT_FILE = "web_form_fields.md"

TEMPERATURE = 0.0


class WebFieldExtractionAgent(BaseAgent[None, PageExtraction]):
    """Extracts the input fields visible on one screen of a web form.

    Like `FormFieldExtractionAgent` it reads a picture, returns the same `PageExtraction` and
    names nothing; unique names are derived afterwards by `services.naming.ensure_unique_names`.

    It is a separate agent because a screen of a website is not a page of a PDF. It arrives with
    the labels the browser measured off the same render, which the prompt has to place; and it
    is surrounded by things a paper form does not have — a main menu, a cookie banner, a step
    indicator, a footer — which the prompt has to exclude.
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
        # NativeOutput for the same reason as the PDF agent: a bare output_type makes
        # pydantic-ai request the result as a tool call, which vLLM rejects unless started
        # with --tool-call-parser. Guided JSON decoding needs no extra server flags.
        return Agent[None, PageExtraction](
            model=model,
            deps_type=NoneType,
            output_type=NativeOutput(PageExtraction),
            instructions=load_instructions(PROMPT_FILE),
        )

    async def extract_screens(self, screens: list[bytes], hints: str = "") -> PageExtraction:
        """Read the form fields visible on a run of consecutive screens.

        Several screens go in one call because a form does not break at a screen boundary. A
        field whose caption sits at the bottom of one screen and whose box is at the top of the
        next is two halves to a model shown one of them, and it duly reports the halves — a
        caption with nothing under it, a box with no caption. Shown both, it sees the field.
        The screens overlap, so the same field also appears twice, and the prompt says to
        report it once.

        Args:
            screens: The consecutive screens as PNG images, in reading order.
            hints: The page's own wording of the labels on those screens, or empty where they
                offered none.

        Returns:
            The fields found, without names.
        """
        # The images go first and the reference after them, in that order deliberately: the
        # question is what the screens show, and the labels are an aid to writing down the
        # answer rather than the thing being read.
        opening = (
            "Extract the input fields visible on these consecutive screens of the form."
            if len(screens) > 1
            else "Extract the input fields visible on this screen of the form."
        )
        prompt: list[str | BinaryContent] = [opening]
        prompt.extend(BinaryContent(data=screen, media_type="image/png") for screen in screens)
        if hints:
            prompt.append(hints)

        # max_tokens is deliberately unset, as in the PDF agent: pinning a number fails
        # whenever it exceeds the server's max_model_len.
        return await self.run(prompt, model_settings={"temperature": TEMPERATURE})

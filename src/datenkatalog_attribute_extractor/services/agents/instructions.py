"""Loading of agent instructions from the bundled prompt files."""

import json
from functools import lru_cache
from importlib import resources

from datenkatalog_attribute_extractor.models.field import PageExtraction

PROMPT_PACKAGE = "datenkatalog_attribute_extractor.prompts"


@lru_cache(maxsize=4)
def load_instructions(prompt_file: str) -> str:
    """Load a prompt and inline the expected output schema.

    vLLM constrains generation to the JSON schema but does not show the model the schema's
    field descriptions, so the schema is repeated in the prompt text.

    Args:
        prompt_file: File name within the prompts package.

    Returns:
        The full instruction text for the agent.
    """
    template = resources.files(PROMPT_PACKAGE).joinpath(prompt_file).read_text(encoding="utf-8")
    schema = json.dumps(PageExtraction.model_json_schema(), indent=2, ensure_ascii=False)
    return template.replace("{schema}", schema)

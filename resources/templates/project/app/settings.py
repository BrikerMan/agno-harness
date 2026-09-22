"""Process settings.

Extend
------
Read AGNO_HARNESS_* here. Agents take their model from model().
"""

from agno.models.openai.like import OpenAILike

from agno_harness import RelayConfig


def model() -> OpenAILike:
    return OpenAILike(
        id=RelayConfig.llm_model(),
        api_key=RelayConfig.llm_api_key() or None,
        base_url=RelayConfig.llm_base_url() or None,
    )

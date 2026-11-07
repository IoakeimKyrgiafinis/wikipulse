"""Every LLM call in WikiPulse goes through this package.

    from wikipulse.llm import Message, get_client

    with get_client("ollama:qwen3:4b") as llm:
        answer = llm.chat("Who won?", system="Answer briefly.", json_schema=SCHEMA)
        answer.data, answer.usage

Callers see only the neutral types below; the provider's own package (`ollama`, or the
optional `anthropic`) is used inside its backend module and nowhere else.
"""

from wikipulse.llm.client import LLMClient, split_thinking
from wikipulse.llm.errors import (
    LLMError,
    LLMUnavailableError,
    ModelNotFoundError,
    StructuredOutputError,
)
from wikipulse.llm.factory import get_client, parse_model_setting
from wikipulse.llm.types import LLMResponse, Message, ToolCall, ToolSpec, Usage

__all__ = [
    "LLMClient",
    "LLMError",
    "LLMResponse",
    "LLMUnavailableError",
    "Message",
    "ModelNotFoundError",
    "StructuredOutputError",
    "ToolCall",
    "ToolSpec",
    "Usage",
    "get_client",
    "parse_model_setting",
    "split_thinking",
]

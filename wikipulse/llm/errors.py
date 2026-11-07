"""Errors raised by `wikipulse.llm`, the same for every backend."""


class LLMError(RuntimeError):
    """Something went wrong while talking to an LLM."""


class LLMUnavailableError(LLMError):
    """The LLM server can't be reached (not running, wrong host) or didn't answer in time."""


class ModelNotFoundError(LLMError):
    """The requested model doesn't exist on the server (for Ollama: it isn't pulled)."""


class StructuredOutputError(LLMError, ValueError):
    """The answer still wasn't valid JSON for the schema after one retry."""

    def __init__(self, message: str, last_text: str) -> None:
        super().__init__(message)
        self.last_text = last_text

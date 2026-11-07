"""`get_client()`: one setting (`backend:model`) picks the backend and the model.

This is the only place that knows which backends exist. Everything else asks for a
client with a setting string, so switching models (or providers) is a change of one
setting, e.g. `DEFAULT_MODEL=ollama:qwen3:8b`.
"""

from wikipulse.config import Settings, get_settings
from wikipulse.llm.client import LLMClient

BACKENDS = ("ollama", "anthropic")


def parse_model_setting(setting: str) -> tuple[str, str]:
    """`ollama:qwen3:8b` -> ("ollama", "qwen3:8b"). Only the first colon separates them,
    because Ollama model names contain colons themselves (name:tag)."""
    backend, _, model = setting.strip().partition(":")
    if backend not in BACKENDS or not model:
        known = ", ".join(f"'{name}:<model>'" for name in BACKENDS)
        raise ValueError(f"Unsupported model setting {setting!r}; use one of {known}")
    return backend, model


def get_client(model_setting: str | None = None, settings: Settings | None = None) -> LLMClient:
    """A client for `model_setting` (default: DEFAULT_MODEL), configured from settings."""
    settings = settings or get_settings()
    backend, model = parse_model_setting(model_setting or settings.default_model)
    if backend == "anthropic":
        from wikipulse.llm.anthropic_backend import AnthropicClient  # optional backend

        key = settings.anthropic_api_key
        return AnthropicClient(
            model, key.get_secret_value() if key else None, timeout_s=settings.llm_timeout_s
        )
    from wikipulse.llm.ollama_backend import OllamaClient

    return OllamaClient(
        model,
        host=settings.ollama_host,
        num_ctx=settings.ollama_num_ctx,
        keep_alive=settings.ollama_keep_alive,
        timeout_s=settings.llm_timeout_s,
    )

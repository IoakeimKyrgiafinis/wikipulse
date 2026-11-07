"""Optional Anthropic backend, for comparison runs against a hosted model.

Never required: the `anthropic` package is imported only when this backend is used
(install it with `uv sync --extra anthropic`), and it needs ANTHROPIC_API_KEY.

Differences from Ollama that this module hides:

- The system prompt is a parameter, not a message.
- Tool results are `tool_result` blocks inside a *user* message, and the assistant's
  tool calls are `tool_use` blocks.
- `max_tokens` is required.
- There is no schema-constrained output here: the schema goes into the system prompt and
  the client checks the answer (with its usual one retry).
- Extended thinking needs a token budget and doesn't allow setting the temperature.
"""

import json
import logging
from typing import Any

from wikipulse.llm.client import ChatRequest, LLMClient
from wikipulse.llm.errors import LLMError, LLMUnavailableError, ModelNotFoundError
from wikipulse.llm.types import LLMResponse, Message, ToolCall, ToolSpec, Usage

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 4096
THINKING_BUDGET = 2048  # tokens of reasoning when `think=True`
SCHEMA_INSTRUCTION = (
    "Answer with only a JSON value that matches this JSON schema, with no other text:\n{schema}"
)


def to_anthropic_tool(tool: ToolSpec) -> dict[str, Any]:
    return {"name": tool.name, "description": tool.description, "input_schema": tool.parameters}


def to_anthropic_content(message: Message) -> list[dict[str, Any]]:
    """The content blocks of one neutral message."""
    if message.role == "tool":
        return [
            {"type": "tool_result", "tool_use_id": message.tool_call_id, "content": message.content}
        ]
    blocks: list[dict[str, Any]] = []
    if message.content:
        blocks.append({"type": "text", "text": message.content})
    for call in message.tool_calls:
        block = {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
        blocks.append(block)
    return blocks


def anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Neutral messages as Anthropic messages: tool results become user turns, and
    consecutive turns of the same role are merged (Anthropic wants them alternating)."""
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = "assistant" if message.role == "assistant" else "user"
        blocks = to_anthropic_content(message)
        if converted and converted[-1]["role"] == role:
            converted[-1]["content"].extend(blocks)
        else:
            converted.append({"role": role, "content": blocks})
    return converted


def anthropic_system(request: ChatRequest) -> str | None:
    """The system prompt, with the schema instruction added for a structured answer."""
    parts = [request.system] if request.system else []
    if request.json_schema is not None:
        parts.append(SCHEMA_INSTRUCTION.format(schema=json.dumps(request.json_schema)))
    return "\n\n".join(parts) or None


def anthropic_arguments(request: ChatRequest, model: str) -> dict[str, Any]:
    """The keyword arguments of `messages.create()` for a request."""
    max_tokens = request.max_tokens or DEFAULT_MAX_TOKENS
    arguments: dict[str, Any] = {
        "model": model,
        "messages": anthropic_messages(request.messages),
        "max_tokens": max_tokens,
    }
    system = anthropic_system(request)
    if system:
        arguments["system"] = system
    if request.tools:
        arguments["tools"] = [to_anthropic_tool(tool) for tool in request.tools]
    if request.think:
        # the thinking budget is part of max_tokens; temperature must stay at its default
        arguments["max_tokens"] = max_tokens + THINKING_BUDGET
        arguments["thinking"] = {"type": "enabled", "budget_tokens": THINKING_BUDGET}
    elif request.temperature is not None:
        arguments["temperature"] = request.temperature
    return arguments


def from_anthropic_response(response: Any, setting: str) -> LLMResponse:
    """An Anthropic message as a neutral LLMResponse."""
    texts: list[str] = []
    thoughts: list[str] = []
    calls: list[ToolCall] = []
    for block in response.content:
        if block.type == "text":
            texts.append(block.text)
        elif block.type == "thinking":
            thoughts.append(block.thinking)
        elif block.type == "tool_use":
            calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input)))
    usage = Usage(
        input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens
    )
    return LLMResponse(
        model=setting,
        text="".join(texts),
        thinking="\n\n".join(thoughts) or None,
        tool_calls=calls,
        stop_reason=response.stop_reason,
        usage=usage,
    )


class AnthropicClient(LLMClient):
    """A Claude model through Anthropic's Messages API."""

    backend = "anthropic"

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        timeout_s: float = 120.0,
        sdk_client: Any = None,  # tests pass a fake with `.messages.create()`
    ) -> None:
        super().__init__(model)
        if sdk_client is None:
            sdk_client = make_sdk_client(api_key, timeout_s)
        self._anthropic = sdk_client

    def _send(self, request: ChatRequest) -> LLMResponse:
        try:
            response = self._anthropic.messages.create(**anthropic_arguments(request, self.model))
        except Exception as error:
            translated = translate_error(error, self.model)
            if translated is None:
                raise
            raise translated from error
        return from_anthropic_response(response, self.setting)

    def close(self) -> None:
        close = getattr(self._anthropic, "close", None)
        if callable(close):
            close()


def make_sdk_client(api_key: str | None, timeout_s: float) -> Any:
    """The `anthropic` SDK client, imported only now so the package stays optional."""
    try:
        import anthropic
    except ImportError as error:
        raise LLMError(
            "The anthropic backend needs the optional `anthropic` package: "
            "run `uv sync --extra anthropic`"
        ) from error
    if not api_key:
        raise LLMError("The anthropic backend needs ANTHROPIC_API_KEY (in .env or the environment)")
    return anthropic.Anthropic(api_key=api_key, timeout=timeout_s)


def translate_error(error: Exception, model: str) -> LLMError | None:
    """The SDK's errors (matched by class name, the SDK is optional) as LLM errors."""
    name = type(error).__name__
    if name == "NotFoundError":
        return ModelNotFoundError(f"Anthropic has no model {model!r}: check the model name")
    if name in {"APIConnectionError", "APITimeoutError"}:
        return LLMUnavailableError(f"Cannot reach the Anthropic API: {error}")
    if name.endswith("Error") and hasattr(error, "status_code"):
        return LLMError(f"Anthropic refused the request for {model!r}: {error}")
    return None

"""Ollama backend: local models through the `ollama` package's native chat API.

Three things matter with local models and are handled here:

- **Context length.** Ollama's default context window is small, and a longer prompt is
  cut off silently (the model just never sees the start). Every request sends
  `num_ctx` (`OLLAMA_NUM_CTX`, 8192 by default). A bigger window needs more GPU memory.
- **Thinking.** Qwen3 thinks before answering unless told not to. `think` is sent on
  every request (off by default); the reasoning comes back in `message.thinking`.
- **Loading.** The first request loads the model into (GPU) memory, which can take
  seconds; `keep_alive` (`OLLAMA_KEEP_ALIVE`) keeps it loaded between calls.
  `Usage.load_s` shows how much of a call was loading.
"""

import logging
from typing import Any

import httpx
import ollama

from wikipulse.llm.client import ChatRequest, LLMClient
from wikipulse.llm.errors import LLMError, LLMUnavailableError, ModelNotFoundError
from wikipulse.llm.types import LLMResponse, Message, ToolCall, ToolSpec, Usage

logger = logging.getLogger(__name__)

NANOSECONDS = 1e9


def to_ollama_message(message: Message) -> dict[str, Any]:
    """A neutral message as an Ollama chat message."""
    converted: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        converted["tool_calls"] = [
            {"function": {"name": call.name, "arguments": call.arguments}}
            for call in message.tool_calls
        ]
    if message.role == "tool" and message.name:
        converted["tool_name"] = message.name  # Ollama matches results to calls by name
    return converted


def to_ollama_tool(tool: ToolSpec) -> dict[str, Any]:
    """A neutral tool as an Ollama (OpenAI-style) function tool."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def ollama_messages(request: ChatRequest) -> list[dict[str, Any]]:
    """The system prompt (as the first message) and the conversation, in Ollama's format."""
    system = [{"role": "system", "content": request.system}] if request.system else []
    return system + [to_ollama_message(message) for message in request.messages]


def ollama_options(request: ChatRequest, num_ctx: int) -> dict[str, Any]:
    """Sampling options: the context length always, the others only when set."""
    options: dict[str, Any] = {"num_ctx": num_ctx}
    if request.temperature is not None:
        options["temperature"] = request.temperature
    if request.max_tokens is not None:
        options["num_predict"] = request.max_tokens
    return options


def from_ollama_response(response: ollama.ChatResponse, setting: str) -> LLMResponse:
    """An Ollama chat response as a neutral LLMResponse (latency is added by the client)."""
    message = response.message
    calls = [
        # Ollama gives tool calls no id; number them so results can refer to them
        ToolCall(id=f"call_{n}", name=call.function.name, arguments=dict(call.function.arguments))
        for n, call in enumerate(message.tool_calls or [])
    ]
    usage = Usage(
        input_tokens=response.prompt_eval_count or 0,
        output_tokens=response.eval_count or 0,
        load_s=(response.load_duration or 0) / NANOSECONDS,
    )
    return LLMResponse(
        model=setting,
        text=message.content or "",
        thinking=message.thinking or None,
        tool_calls=calls,
        stop_reason=response.done_reason,
        usage=usage,
    )


class OllamaClient(LLMClient):
    """A model served by a local Ollama server."""

    backend = "ollama"

    def __init__(
        self,
        model: str,
        host: str = "http://localhost:11434",
        num_ctx: int = 8192,
        keep_alive: str = "10m",
        timeout_s: float = 120.0,
        transport: httpx.BaseTransport | None = None,  # tests pass a mock transport
    ) -> None:
        super().__init__(model)
        self.host = host
        self.num_ctx = num_ctx
        self.keep_alive = keep_alive
        self.timeout_s = timeout_s
        extra = {"transport": transport} if transport is not None else {}
        self._ollama = ollama.Client(host=host, timeout=timeout_s, **extra)

    def _send(self, request: ChatRequest) -> LLMResponse:
        try:
            response = self._ollama.chat(
                model=self.model,
                messages=ollama_messages(request),
                tools=[to_ollama_tool(tool) for tool in request.tools] or None,
                format=request.json_schema,
                think=request.think,
                options=ollama_options(request, self.num_ctx),
                keep_alive=self.keep_alive,
                stream=False,
            )
        except (ollama.ResponseError, ConnectionError, httpx.HTTPError) as error:
            translated = self._translate(error)
            if translated is None:
                raise
            raise translated from error
        return from_ollama_response(response, self.setting)

    def list_models(self) -> list[str]:
        """Names of the models pulled on this Ollama server, e.g. `qwen3:4b`."""
        try:
            listing = self._ollama.list()
        except (ollama.ResponseError, ConnectionError, httpx.HTTPError) as error:
            translated = self._translate(error)
            if translated is None:
                raise
            raise translated from error
        return sorted(model.model for model in listing.models if model.model)

    def close(self) -> None:
        self._ollama.close()

    def _translate(self, error: Exception) -> LLMError | None:
        """The `ollama` package's error as a WikiPulse LLM error with a hint (None: unknown)."""
        if isinstance(error, ollama.ResponseError):
            if error.status_code == 404:
                return ModelNotFoundError(
                    f"Ollama has no model {self.model!r}: run `ollama pull {self.model}`, "
                    "or choose a model you have (`ollama list`)"
                )
            return LLMError(f"Ollama refused the request for {self.model!r}: {error.error}")
        if isinstance(error, httpx.TimeoutException):  # before TransportError, its parent
            return LLMUnavailableError(
                f"Ollama gave no answer within {self.timeout_s:g} s (LLM_TIMEOUT_S)"
            )
        if isinstance(error, ConnectionError | httpx.TransportError):
            return LLMUnavailableError(
                f"Cannot reach Ollama at {self.host}: is it running (`ollama serve`, or the "
                "Ollama app)? Set OLLAMA_HOST if it listens elsewhere."
            )
        return None

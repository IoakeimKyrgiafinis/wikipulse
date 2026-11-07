"""The provider-neutral LLM client: `LLMClient.chat()` and `get_client()`.

Everything that is the same for every provider lives here: turning a plain string into
a message, timing the call and adding up token usage, removing "thinking" text from the
answer, and checking a structured (JSON-schema) answer with one retry. A backend only
implements `_send()`: convert the request to its own API, call it, convert the answer
back. See `wikipulse/llm/ollama_backend.py` and
`wikipulse/llm/anthropic_backend.py`; `get_client()` is in `wikipulse/llm/factory.py`.
"""

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from types import TracebackType
from typing import Any, Self

from wikipulse.llm.errors import StructuredOutputError
from wikipulse.llm.schema import schema_errors
from wikipulse.llm.types import LLMResponse, Message, ToolSpec, Usage

logger = logging.getLogger(__name__)

# Sent after an answer that wasn't valid JSON for the schema, so the retry isn't just the
# same request again (at temperature 0 that would likely give the same broken answer).
RETRY_PROMPT = (
    "Your answer was not valid: {error}. Answer again with only a JSON value that "
    "matches this JSON schema, and nothing else:\n{schema}"
)

_THINK_BLOCK = re.compile(r"<think>(.*?)</think>", re.DOTALL)
_JSON_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


@dataclass(frozen=True)
class ChatRequest:
    """Everything a backend needs for one request (already normalized by `chat()`)."""

    messages: list[Message]
    system: str | None = None
    tools: list[ToolSpec] = field(default_factory=list)
    json_schema: dict[str, Any] | None = None
    temperature: float | None = 0.0
    max_tokens: int | None = None
    think: bool = False


def split_thinking(text: str) -> tuple[str, str | None]:
    """(answer, thinking) from a text that may contain `<think>...</think>` blocks.

    Reasoning models such as Qwen3 write their reasoning between these tags. Some
    templates put the opening tag in the prompt, so a lone `</think>` also ends the
    thinking; an unclosed `<think>` (the answer was cut off) is all thinking.
    """
    thoughts = [part.strip() for part in _THINK_BLOCK.findall(text)]
    answer = _THINK_BLOCK.sub("", text)
    if "</think>" in answer:
        before, _, answer = answer.rpartition("</think>")
        thoughts.insert(0, before.strip())
    if "<think>" in answer:
        answer, _, rest = answer.partition("<think>")
        thoughts.append(rest.strip())
    thinking = "\n\n".join(part for part in thoughts if part)
    return answer.strip(), thinking or None


def parse_json(text: str, schema: dict[str, Any]) -> tuple[Any, str | None]:
    """(data, None) if `text` is JSON valid for `schema`, else (None, what is wrong)."""
    fenced = _JSON_FENCE.match(text.strip())
    try:
        data = json.loads(fenced.group(1) if fenced else text)
    except json.JSONDecodeError as error:
        return None, f"not JSON ({error})"
    errors = schema_errors(data, schema)
    if errors:
        return None, "; ".join(errors[:5])
    return data, None


class LLMClient(ABC):
    """One model behind one backend. Create it with `get_client()`.

    `total_usage` adds up the usage of every call made with this client.
    """

    backend: str = ""

    def __init__(self, model: str) -> None:
        self.model = model
        self.total_usage = Usage()

    @property
    def setting(self) -> str:
        """The `backend:model` setting of this client, e.g. `ollama:qwen3:4b`."""
        return f"{self.backend}:{self.model}"

    def chat(
        self,
        messages: str | Sequence[Message],
        system: str | None = None,
        tools: Sequence[ToolSpec] | None = None,
        json_schema: dict[str, Any] | None = None,
        temperature: float | None = 0.0,
        max_tokens: int | None = None,
        think: bool = False,
    ) -> LLMResponse:
        """Ask the model and return its answer.

        - `messages`: the conversation so far, or a plain string for one user message.
        - `system`: instructions that frame the whole conversation.
        - `tools`: tools the model may call; calls come back in `response.tool_calls`.
        - `json_schema`: the answer must be JSON valid for this schema; it is parsed into
          `response.data`. An invalid answer is retried once, then StructuredOutputError.
        - `temperature`: 0 (the default) is as repeatable as possible; None leaves the
          model's own default.
        - `max_tokens`: most tokens to generate (None: the backend's default).
        - `think`: let a reasoning model think first (slower); its reasoning goes to
          `response.thinking`, never into `response.text`.
        """
        conversation = [Message.user(messages)] if isinstance(messages, str) else list(messages)
        request = ChatRequest(
            conversation, system, list(tools or []), json_schema, temperature, max_tokens, think
        )
        response = self._ask(request)
        if json_schema is None or response.tool_calls:
            return response
        response.data, error = parse_json(response.text, json_schema)
        if error is None:
            return response
        logger.warning("%s gave an invalid structured answer (%s); asking again", self, error)
        retry = Message.user(RETRY_PROMPT.format(error=error, schema=json.dumps(json_schema)))
        followup = [*conversation, Message.assistant(response.text), retry]
        second = self._ask(replace(request, messages=followup))
        second.usage = response.usage + second.usage
        second.data, error = parse_json(second.text, json_schema)
        if error is not None:
            raise StructuredOutputError(
                f"{self.setting} gave no valid JSON for the schema after a retry: {error}",
                last_text=second.text,
            )
        return second

    def _ask(self, request: ChatRequest) -> LLMResponse:
        """One request: send it, time it, separate the thinking, record the usage."""
        started = time.perf_counter()
        response = self._send(request)
        response.usage.latency_s = time.perf_counter() - started
        response.usage.calls = 1
        text, thinking = split_thinking(response.text)
        parts = [part for part in (response.thinking, thinking) if part]
        response.text = text
        response.thinking = "\n\n".join(parts) or None
        self.total_usage = self.total_usage + response.usage
        usage = response.usage
        logger.debug(
            "%s: %d tokens in, %d out, %.2f s (load %.2f s), stop %s",
            self.setting,
            usage.input_tokens,
            usage.output_tokens,
            usage.latency_s,
            usage.load_s,
            response.stop_reason,
        )
        return response

    @abstractmethod
    def _send(self, request: ChatRequest) -> LLMResponse:
        """Call the provider. Fill text, thinking, tool calls, tokens and stop reason."""

    def list_models(self) -> list[str]:
        """Names of the models this backend can use right now (if it can tell)."""
        raise NotImplementedError(f"{self.backend} can't list its models")

    def close(self) -> None:  # noqa: B027 (optional to override)
        """Release connections. The client can't be used afterwards."""

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __str__(self) -> str:
        return self.setting

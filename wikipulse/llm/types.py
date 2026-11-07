"""Provider-neutral types for LLM calls.

Every backend (Ollama, Anthropic, ...) converts these to and from its own format, so the
rest of WikiPulse never sees a provider's request or response objects.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["user", "assistant", "tool"]


class ToolCall(BaseModel):
    """The model asks the caller to run a tool with these arguments."""

    model_config = ConfigDict(frozen=True)

    id: str  # links the tool's result (a `tool` message) back to this call
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    """One turn of a conversation.

    The system prompt is not a message: it is passed to `chat()` separately, because
    providers treat it differently (Ollama as a first message, Anthropic as a parameter).

    - `user`: text from the user (or from our code acting as the user).
    - `assistant`: an earlier answer of the model, possibly with `tool_calls`.
    - `tool`: the result of running a tool; `tool_call_id` and `name` say which call.
    """

    model_config = ConfigDict(frozen=True)

    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None  # tool name, for `tool` messages

    @classmethod
    def user(cls, content: str) -> "Message":
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str, tool_calls: list[ToolCall] | None = None) -> "Message":
        return cls(role="assistant", content=content, tool_calls=tool_calls or [])

    @classmethod
    def tool_result(cls, call: ToolCall, content: str) -> "Message":
        return cls(role="tool", content=content, tool_call_id=call.id, name=call.name)


class ToolSpec(BaseModel):
    """A tool the model may call: a name, what it does, and a JSON schema of its arguments."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})


class Usage(BaseModel):
    """What one `chat()` call cost: tokens and time (summed over retries)."""

    input_tokens: int = 0  # prompt tokens the model read
    output_tokens: int = 0  # tokens it generated (thinking included)
    latency_s: float = 0.0  # wall-clock time of the whole call
    load_s: float = 0.0  # part of it spent loading the model into memory (Ollama only)
    calls: int = 0  # requests sent (2 when a structured answer was retried)

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            latency_s=self.latency_s + other.latency_s,
            load_s=self.load_s + other.load_s,
            calls=self.calls + other.calls,
        )


class LLMResponse(BaseModel):
    """The answer of one `chat()` call."""

    model: str  # the `backend:model` setting that answered
    text: str  # the answer, with any thinking removed
    thinking: str | None = None  # the model's reasoning, if it produced any
    tool_calls: list[ToolCall] = Field(default_factory=list)
    data: Any = None  # the parsed JSON when `json_schema` was given
    stop_reason: str | None = None  # e.g. "stop", "length", "end_turn", "tool_use"
    usage: Usage = Field(default_factory=Usage)

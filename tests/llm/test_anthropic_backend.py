"""The optional Anthropic backend with a fake SDK client (the package isn't needed)."""

from types import SimpleNamespace
from typing import Any

import pytest

from wikipulse.config import Settings
from wikipulse.llm import LLMError, Message, ModelNotFoundError, ToolCall, ToolSpec, get_client
from wikipulse.llm.anthropic_backend import AnthropicClient


def block(kind: str, **fields: Any) -> SimpleNamespace:
    return SimpleNamespace(type=kind, **fields)


def message(*blocks: SimpleNamespace, stop: str = "end_turn") -> SimpleNamespace:
    usage = SimpleNamespace(input_tokens=30, output_tokens=12)
    return SimpleNamespace(content=list(blocks), stop_reason=stop, usage=usage)


class FakeSDK:
    """Stands in for `anthropic.Anthropic`: records `messages.create()` calls."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **arguments: Any) -> Any:
        self.calls.append(arguments)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_request_format() -> None:
    sdk = FakeSDK(message(block("text", text="Paris")))
    client = AnthropicClient("claude-x", sdk_client=sdk)
    response = client.chat("Capital of France?", system="Answer briefly.")
    assert sdk.calls[0] == {
        "model": "claude-x",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Capital of France?"}]}],
        "max_tokens": 4096,
        "system": "Answer briefly.",
        "temperature": 0.0,
    }
    assert response.text == "Paris" and response.model == "anthropic:claude-x"
    assert (response.usage.input_tokens, response.usage.output_tokens) == (30, 12)


def test_tool_calls_and_results_are_converted() -> None:
    tool = ToolSpec(name="lookup", description="Look up a page.", parameters={"type": "object"})
    use = block("tool_use", id="toolu_1", name="lookup", input={"title": "Oslo"})
    sdk = FakeSDK(message(block("text", text="Let me look."), use, stop="tool_use"))
    client = AnthropicClient("claude-x", sdk_client=sdk)
    response = client.chat("Oslo?", tools=[tool])
    assert sdk.calls[0]["tools"] == [
        {"name": "lookup", "description": "Look up a page.", "input_schema": {"type": "object"}}
    ]
    call = ToolCall(id="toolu_1", name="lookup", arguments={"title": "Oslo"})
    assert response.tool_calls == [call] and response.stop_reason == "tool_use"

    sdk.replies.append(message(block("text", text="Oslo is in Norway.")))
    conversation = [
        Message.user("Oslo?"),
        Message.assistant("Let me look.", [call]),
        Message.tool_result(call, "Capital of Norway"),
    ]
    client.chat(conversation, tools=[tool])
    assert sdk.calls[1]["messages"][1:] == [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me look."},
                {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"title": "Oslo"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "Capital of Norway"}
            ],
        },
    ]


def test_schema_goes_into_the_system_prompt_and_is_checked() -> None:
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    sdk = FakeSDK(
        message(block("text", text='{"n": "two"}')), message(block("text", text='{"n": 2}'))
    )
    response = AnthropicClient("claude-x", sdk_client=sdk).chat("Count", json_schema=schema)
    assert response.data == {"n": 2} and len(sdk.calls) == 2
    assert '"required": ["n"]' in sdk.calls[0]["system"]


def test_thinking_uses_a_budget_and_comes_back_separately() -> None:
    sdk = FakeSDK(message(block("thinking", thinking="Hmm."), block("text", text="42")))
    response = AnthropicClient("claude-x", sdk_client=sdk).chat("?", max_tokens=100, think=True)
    arguments = sdk.calls[0]
    assert arguments["thinking"] == {"type": "enabled", "budget_tokens": 2048}
    assert arguments["max_tokens"] == 2148 and "temperature" not in arguments
    assert (response.text, response.thinking) == ("42", "Hmm.")


def test_sdk_errors_are_translated() -> None:
    NotFoundError = type("NotFoundError", (Exception,), {})  # noqa: N806 (a class)
    sdk = FakeSDK(NotFoundError("no such model"))
    with pytest.raises(ModelNotFoundError, match="claude-nope"):
        AnthropicClient("claude-nope", sdk_client=sdk).chat("?")


def test_the_backend_is_optional_and_needs_a_key() -> None:
    settings = Settings(_env_file=None, anthropic_api_key=None)
    with pytest.raises(LLMError, match="anthropic"):
        # without the package: "uv sync --extra anthropic"; with it: "needs ANTHROPIC_API_KEY"
        get_client("anthropic:claude-x", settings)

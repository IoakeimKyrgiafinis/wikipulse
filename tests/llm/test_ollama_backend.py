"""The Ollama backend over a mocked HTTP transport: requests, conversions, errors."""

import httpx
import pytest

from tests.llm.helpers import mock_ollama, ollama_reply
from wikipulse.llm import (
    LLMError,
    LLMUnavailableError,
    Message,
    ModelNotFoundError,
    ToolCall,
    ToolSpec,
)
from wikipulse.llm.check import check_models
from wikipulse.llm.ollama_backend import OllamaClient

WEATHER = ToolSpec(
    name="get_weather",
    description="Current weather in a city.",
    parameters={
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
)


def test_request_sends_context_length_thinking_switch_and_keep_alive() -> None:
    client, seen = mock_ollama([ollama_reply("Paris")])
    client.chat("Capital of France?", system="Answer briefly.")
    body = seen[0]
    assert body["model"] == "qwen3:4b"
    assert body["messages"] == [
        {"role": "system", "content": "Answer briefly."},
        {"role": "user", "content": "Capital of France?"},
    ]
    assert body["options"] == {"num_ctx": 8192, "temperature": 0.0}
    assert body["think"] is False
    assert body["stream"] is False
    assert body["keep_alive"] == "10m"
    assert "format" not in body and not body.get("tools")


def test_optional_settings_reach_ollama() -> None:
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    client, seen = mock_ollama([ollama_reply('{"a": "b"}')])
    client.chat("?", json_schema=schema, temperature=None, max_tokens=50, think=True)
    body = seen[0]
    assert body["format"] == schema
    assert body["options"] == {"num_ctx": 8192, "num_predict": 50}  # model's own temperature
    assert body["think"] is True


def test_response_text_thinking_and_usage() -> None:
    reply = ollama_reply("Paris", message={"thinking": "France's capital..."})
    client, _ = mock_ollama([reply])
    response = client.chat("Capital of France?")
    assert response.text == "Paris"
    assert response.thinking == "France's capital..."
    assert response.model == "ollama:qwen3:4b"
    assert response.stop_reason == "stop"
    usage = response.usage
    assert (usage.input_tokens, usage.output_tokens, usage.load_s) == (42, 7, 0.5)


def test_thinking_leaked_into_the_content_is_moved_out() -> None:
    # thinking-only models write their reasoning and `</think>` even with thinking off
    client, _ = mock_ollama([ollama_reply("The user wants one word.\n</think>\n\npong")])
    response = client.chat("Say pong")
    assert response.text == "pong"
    assert response.thinking == "The user wants one word."


def test_tools_are_converted_both_ways() -> None:
    call = {"function": {"name": "get_weather", "arguments": {"city": "Oslo"}}}
    client, seen = mock_ollama([ollama_reply("", message={"tool_calls": [call]})])
    response = client.chat("Weather in Oslo?", tools=[WEATHER])
    assert seen[0]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Current weather in a city.",
                "parameters": WEATHER.parameters,
            },
        }
    ]
    assert response.tool_calls == [
        ToolCall(id="call_0", name="get_weather", arguments={"city": "Oslo"})
    ]


def test_a_tool_conversation_is_sent_in_ollamas_format() -> None:
    client, seen = mock_ollama([ollama_reply("It is 4 degrees in Oslo.")])
    call = ToolCall(id="call_0", name="get_weather", arguments={"city": "Oslo"})
    conversation = [
        Message.user("Weather in Oslo?"),
        Message.assistant("", [call]),
        Message.tool_result(call, '{"celsius": 4}'),
    ]
    assert client.chat(conversation, tools=[WEATHER]).text == "It is 4 degrees in Oslo."
    assert seen[0]["messages"][1:] == [
        {
            "role": "assistant",  # the `ollama` package leaves out the empty content
            "tool_calls": [{"function": {"name": "get_weather", "arguments": {"city": "Oslo"}}}],
        },
        {"role": "tool", "content": '{"celsius": 4}', "tool_name": "get_weather"},
    ]


def test_a_tool_call_skips_the_schema_check() -> None:
    call = {"function": {"name": "get_weather", "arguments": {"city": "Oslo"}}}
    client, seen = mock_ollama([ollama_reply("", message={"tool_calls": [call]})])
    response = client.chat("?", tools=[WEATHER], json_schema={"type": "object"})
    assert response.tool_calls and response.data is None and len(seen) == 1


def test_a_missing_model_suggests_pulling_it() -> None:
    def not_found(body: dict) -> httpx.Response:
        return httpx.Response(404, json={"error": "model 'qwen3:8b' not found"})

    client, _ = mock_ollama(not_found, model="qwen3:8b")
    with pytest.raises(ModelNotFoundError, match="ollama pull qwen3:8b"):
        client.chat("?")


def test_other_ollama_errors_are_llm_errors() -> None:
    def refuse(body: dict) -> httpx.Response:
        return httpx.Response(400, json={"error": "qwen3:4b does not support tools"})

    client, _ = mock_ollama(refuse)
    with pytest.raises(LLMError, match="does not support tools"):
        client.chat("?", tools=[WEATHER])


def test_an_unreachable_server_says_how_to_start_it() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = OllamaClient("qwen3:4b", host="http://nowhere:1", transport=httpx.MockTransport(down))
    with pytest.raises(LLMUnavailableError, match="Cannot reach Ollama at http://nowhere:1"):
        client.chat("?")
    with pytest.raises(LLMUnavailableError):
        client.list_models()


def test_a_timeout_names_the_setting() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    client = OllamaClient("m", timeout_s=5, transport=httpx.MockTransport(slow))
    with pytest.raises(LLMUnavailableError, match="within 5 s .LLM_TIMEOUT_S"):
        client.chat("?")


def test_list_models() -> None:
    def tags(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"model": "qwen3:8b"}, {"model": "qwen3:4b"}]})

    client = OllamaClient("qwen3:4b", transport=httpx.MockTransport(tags))
    assert client.list_models() == ["qwen3:4b", "qwen3:8b"]


def test_check_tells_how_to_pull_a_missing_model(capsys: pytest.CaptureFixture[str]) -> None:
    def tags(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"model": "qwen3:4b"}]})

    client = OllamaClient("qwen3:8b", transport=httpx.MockTransport(tags))
    assert check_models(client) is False
    assert "ollama pull qwen3:8b" in capsys.readouterr().out

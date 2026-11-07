"""The neutral client: setting parsing, thinking removal, schema retry, usage, schemas."""

import pytest

from tests.llm.helpers import ScriptedClient
from wikipulse.config import Settings
from wikipulse.llm import (
    Message,
    StructuredOutputError,
    get_client,
    parse_model_setting,
    split_thinking,
)
from wikipulse.llm.ollama_backend import OllamaClient
from wikipulse.llm.schema import schema_errors

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": ["news", "not news"]},
        "score": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["label"],
}


# --- settings ---------------------------------------------------------------------


def test_model_setting_splits_at_the_first_colon_only() -> None:
    assert parse_model_setting("ollama:qwen3:8b") == ("ollama", "qwen3:8b")
    assert parse_model_setting(" anthropic:claude-x ") == ("anthropic", "claude-x")


@pytest.mark.parametrize("setting", ["qwen3:4b", "ollama:", "openai:gpt", ""])
def test_unknown_or_incomplete_settings_are_rejected(setting: str) -> None:
    with pytest.raises(ValueError, match="Unsupported model setting"):
        parse_model_setting(setting)


def test_get_client_uses_the_settings() -> None:
    settings = Settings(
        _env_file=None,
        default_model="ollama:qwen3:4b",
        ollama_host="http://gpu-box:11434",
        ollama_num_ctx=4096,
        ollama_keep_alive="1h",
        llm_timeout_s=30,
    )
    with get_client(settings=settings) as client:
        assert isinstance(client, OllamaClient)
        assert client.setting == "ollama:qwen3:4b"
        assert (client.host, client.num_ctx, client.keep_alive) == (
            "http://gpu-box:11434",
            4096,
            "1h",
        )
        assert client.timeout_s == 30
    with get_client("ollama:qwen3:8b", settings) as client:
        assert client.model == "qwen3:8b"


def test_llm_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.default_model == "ollama:qwen3:4b"
    assert settings.ollama_num_ctx == 8192
    assert settings.ollama_keep_alive == "10m"
    assert settings.llm_timeout_s == 120
    assert settings.anthropic_api_key is None


# --- thinking -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "answer", "thinking"),
    [
        ("pong", "pong", None),
        ("<think>a user asks</think>\n\npong", "pong", "a user asks"),
        ("<think>\n\n</think>\n\npong", "pong", None),  # thinking switched off
        ("Hmm, one word.\n</think>\n\npong", "pong", "Hmm, one word."),  # opening tag in prompt
        ("<think>still thinking when cut off", "", "still thinking when cut off"),
        ("<think>one</think>A<think>two</think>B", "AB", "one\n\ntwo"),
    ],
)
def test_thinking_is_split_from_the_answer(text: str, answer: str, thinking: str | None) -> None:
    assert split_thinking(text) == (answer, thinking)


def test_chat_moves_thinking_out_of_the_text() -> None:
    client = ScriptedClient(["<think>let me see</think>The answer."])
    response = client.chat("Question?")
    assert response.text == "The answer."
    assert response.thinking == "let me see"


# --- requests and usage -------------------------------------------------------------


def test_a_string_becomes_one_user_message_and_defaults_are_sent() -> None:
    client = ScriptedClient(["ok"])
    client.chat("Hello", system="Be brief.")
    request = client.requests[0]
    assert request.messages == [Message.user("Hello")]
    assert request.system == "Be brief."
    assert request.temperature == 0.0 and request.think is False and request.tools == []


def test_usage_is_recorded_per_call_and_in_total() -> None:
    client = ScriptedClient(["a", "b"])
    first = client.chat("1")
    client.chat("2")
    assert first.usage.input_tokens == 10 and first.usage.output_tokens == 5
    assert first.usage.calls == 1 and first.usage.latency_s >= 0
    assert client.total_usage.calls == 2 and client.total_usage.input_tokens == 20


# --- structured answers -------------------------------------------------------------


def test_a_valid_structured_answer_is_parsed() -> None:
    client = ScriptedClient(['{"label": "news", "score": 0.9}'])
    response = client.chat("Is it news?", json_schema=ANSWER_SCHEMA)
    assert response.data == {"label": "news", "score": 0.9}
    assert client.requests[0].json_schema == ANSWER_SCHEMA


def test_a_fenced_json_answer_is_accepted() -> None:
    client = ScriptedClient(['```json\n{"label": "news"}\n```'])
    assert client.chat("?", json_schema=ANSWER_SCHEMA).data == {"label": "news"}


def test_an_invalid_structured_answer_is_retried_once_with_the_error() -> None:
    client = ScriptedClient(['{"label": "maybe"}', '{"label": "not news"}'])
    response = client.chat("Is it news?", json_schema=ANSWER_SCHEMA)
    assert response.data == {"label": "not news"}
    assert len(client.requests) == 2
    retry = client.requests[1].messages
    assert retry[0] == Message.user("Is it news?")
    assert retry[1] == Message.assistant('{"label": "maybe"}')
    assert "'maybe' is not one of" in retry[2].content and '"enum"' in retry[2].content
    assert response.usage.calls == 2 and response.usage.input_tokens == 20


def test_a_second_invalid_answer_raises_a_clear_error() -> None:
    client = ScriptedClient(["not json", '{"score": 2}'])
    with pytest.raises(StructuredOutputError, match="after a retry") as caught:
        client.chat("?", json_schema=ANSWER_SCHEMA)
    assert caught.value.last_text == '{"score": 2}'
    assert isinstance(caught.value, ValueError)  # callers may catch it as a ValueError
    assert len(client.requests) == 2


def test_thinking_is_removed_before_the_json_is_parsed() -> None:
    client = ScriptedClient(['<think>{"label": "x"}</think>{"label": "news"}'])
    assert client.chat("?", json_schema=ANSWER_SCHEMA).data == {"label": "news"}


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        ({"label": "news"}, None),
        ([], "expected object"),
        ({}, "missing 'label'"),
        ({"label": "news", "score": "high"}, "expected number"),
        ({"label": "news", "score": True}, "expected number"),  # a bool is no number in JSON
        ({"label": "news", "score": 1.5}, "above 1"),
    ],
)
def test_schema_check(value: object, problem: str | None) -> None:
    errors = schema_errors(value, ANSWER_SCHEMA)
    assert (errors == []) if problem is None else any(problem in e for e in errors)


def test_schema_check_of_arrays_nullable_types_and_closed_objects() -> None:
    schema = {
        "type": "array",
        "minItems": 1,
        "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "id": {"type": ["string", "null"]}},
            "additionalProperties": False,
        },
    }
    assert schema_errors([{"name": "Ada", "id": None}], schema) == []
    assert schema_errors([], schema) == ["$: length 0 is below 1"]
    assert schema_errors([{"name": "Ada", "x": 1}], schema) == ["$[0]: unexpected key 'x'"]
    assert schema_errors([{"id": 3}], schema) == ["$[0].id: expected string or null, got int"]

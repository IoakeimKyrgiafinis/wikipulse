"""Live tests against the local Ollama (DEFAULT_MODEL): `uv run pytest -m live tests/llm`.

They need Ollama running with the model pulled; watch Task Manager's GPU view while they
run to see the model's memory use with the 8192-token context.
"""

from collections.abc import Iterator

import pytest

from wikipulse.llm import LLMClient, Message, ToolSpec, get_client

pytestmark = pytest.mark.live

ADD = ToolSpec(
    name="add",
    description="Add two integers and return their sum.",
    parameters={
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
    },
)


@pytest.fixture(scope="module")
def llm() -> Iterator[LLMClient]:
    with get_client() as client:
        yield client


def test_plain_answer(llm: LLMClient) -> None:
    response = llm.chat("What is the capital of France? Answer with the city name only.")
    assert "paris" in response.text.lower()
    assert "<think>" not in response.text and "</think>" not in response.text
    assert response.usage.input_tokens > 0 and response.usage.output_tokens > 0
    assert response.usage.latency_s > 0


def test_json_schema_answer(llm: LLMClient) -> None:
    schema = {
        "type": "object",
        "properties": {
            "city": {"type": "string"},
            "country": {"type": "string"},
            "is_capital": {"type": "boolean"},
        },
        "required": ["city", "country", "is_capital"],
    }
    response = llm.chat(
        "Describe Lyon.",
        system="Answer with JSON: the city, its country, and whether it is the capital.",
        json_schema=schema,
    )
    assert response.data["country"].lower() == "france"
    assert response.data["is_capital"] is False


def test_tool_call(llm: LLMClient) -> None:
    question = Message.user("What is 1234 + 4321? Use the add tool.")
    first = llm.chat([question], tools=[ADD])
    assert len(first.tool_calls) >= 1
    call = first.tool_calls[0]
    numbers = {int(call.arguments["a"]), int(call.arguments["b"])}
    assert call.name == "add" and numbers == {1234, 4321}
    result = Message.tool_result(call, str(sum(numbers)))
    second = llm.chat(
        [question, Message.assistant(first.text, first.tool_calls), result], tools=[ADD]
    )
    assert "5555" in second.text.replace(",", "")

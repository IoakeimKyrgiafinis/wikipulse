"""The MCP server in-process, through the SDK's own client: every tool, the resource,
the prompts and the error cases, as an MCP client sees them."""

import json

import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError

from tests.mcp_server.helpers import CRISIS, DownGraph, ScriptedAsker, ToolGraph
from wikipulse.llm.errors import LLMUnavailableError
from wikipulse.mcp_server.explore import Explorer
from wikipulse.mcp_server.server import GRAPH_DOWN, build_server, check_host

TOOLS = {"get_trending_events", "get_event", "ask", "find_connection", "search_entities"}


def server_with(graph=None, asker=None):
    return build_server(Explorer(graph or ToolGraph(), asker or ScriptedAsker()))


async def call(server, tool: str, arguments: dict) -> tuple[bool, str, dict | None]:
    async with Client(server) as client:
        result = await client.call_tool(tool, arguments)
    return bool(result.is_error), result.content[0].text, result.structured_content


async def test_lists_every_tool_resource_and_prompt() -> None:
    async with Client(server_with()) as client:
        tools = (await client.list_tools()).tools
        templates = (await client.list_resource_templates()).resource_templates
        prompts = (await client.list_prompts()).prompts
        info = client.server_info
    assert {t.name for t in tools} == TOOLS
    assert [t.uri_template for t in templates] == ["event://{event_id}"]
    assert {p.name: [a.name for a in p.arguments or []] for p in prompts} == {
        "daily_briefing": [],
        "explain_event": ["event_id"],
    }
    assert info.name == "wikipulse"


async def test_every_tool_is_annotated_read_only_with_a_description_and_schemas() -> None:
    async with Client(server_with()) as client:
        tools = (await client.list_tools()).tools
    for tool in tools:
        assert tool.annotations.read_only_hint is True, tool.name
        assert tool.annotations.destructive_hint is False, tool.name
        assert tool.annotations.open_world_hint is False, tool.name
        assert len(tool.description) > 100, tool.name
        assert tool.output_schema is not None, tool.name
    by_name = {t.name: t for t in tools}
    assert by_name["ask"].annotations.idempotent_hint is False  # an LLM answers
    limit = by_name["get_trending_events"].input_schema["properties"]["limit"]
    assert (limit["minimum"], limit["maximum"], limit["default"]) == (1, 25, 10)


async def test_get_trending_events() -> None:
    error, text, data = await call(server_with(), "get_trending_events", {"limit": 1})
    assert not error
    assert [e["event_id"] for e in data["events"]] == [CRISIS.key]
    assert json.loads(text) == data  # the same JSON as text, for clients without structure


async def test_get_event() -> None:
    graph = ToolGraph(briefings={CRISIS.key: "A briefing."})
    error, _, data = await call(server_with(graph), "get_event", {"event_id": "341"})
    assert not error
    assert data["event"]["title"] == CRISIS.title and data["briefing"] == "A briefing."
    assert data["entities"][0]["source"]["url"].endswith("oldid=7")


async def test_ask() -> None:
    asker = ScriptedAsker()
    error, _, data = await call(server_with(asker=asker), "ask", {"question": "Why?"})
    assert not error and asker.questions == ["Why?"]
    assert data["sources"][0]["title"] == "Ueno Bank" and data["answerable"] is True


async def test_find_connection() -> None:
    arguments = {"entity_a": "Ueno Bank", "entity_b": "Q2"}
    error, _, data = await call(server_with(), "find_connection", arguments)
    assert not error
    assert data["entity_b"]["name"] == "Santiago Peña"
    assert data["paths"][0]["steps"][0]["source"]["revision"] == 101


async def test_search_entities() -> None:
    error, _, data = await call(server_with(), "search_entities", {"name": "Peña"})
    assert not error
    assert [(e["entity_id"], e["exact"]) for e in data["entities"]] == [("Q2", True)]


@pytest.mark.parametrize(
    ("tool", "arguments", "message"),
    [
        ("get_event", {"event_id": "run:999"}, "get_trending_events"),
        ("get_event", {"event_id": "Ueno Bank"}, "not an event id"),
        ("find_connection", {"entity_a": "Bank", "entity_b": "Peña"}, "Close matches"),
        ("find_connection", {"entity_a": "Peña", "entity_b": "Q2"}, "same entity"),
        ("ask", {"question": "  "}, "The question is empty"),
        ("search_entities", {"name": ""}, "The name is empty"),
    ],
)
async def test_bad_requests_return_errors_that_say_what_to_do(
    tool: str, arguments: dict, message: str
) -> None:
    error, text, _ = await call(server_with(), tool, arguments)
    assert error and message in text


@pytest.mark.parametrize(
    ("tool", "arguments", "field"),
    [
        ("get_trending_events", {"limit": 100}, "less than or equal to 25"),
        ("get_trending_events", {"hours": 0}, "greater than 0"),
        ("ask", {"question": "x" * 501}, "at most 500 characters"),
        ("get_event", {}, "Field required"),
        ("search_entities", {"name": "x", "limit": 0}, "greater than or equal to 1"),
    ],
)
async def test_arguments_outside_the_limits_are_rejected(
    tool: str, arguments: dict, field: str
) -> None:
    error, text, _ = await call(server_with(), tool, arguments)
    assert error and field in text


async def test_a_graph_that_is_down_gives_one_clear_message() -> None:
    server = server_with(graph=DownGraph())
    for tool, arguments in [
        ("get_trending_events", {}),
        ("get_event", {"event_id": "run:1"}),
        ("search_entities", {"name": "Peña"}),
        ("find_connection", {"entity_a": "Ueno Bank", "entity_b": "Peña"}),
    ]:
        error, text, _ = await call(server, tool, arguments)
        assert error and text.endswith(GRAPH_DOWN), tool


async def test_a_failing_model_points_to_the_other_tools() -> None:
    asker = ScriptedAsker(error=LLMUnavailableError("Ollama is not running"))
    error, text, _ = await call(server_with(asker=asker), "ask", {"question": "Why?"})
    assert error and "Ollama is not running" in text and "get_event" in text


async def test_event_resource() -> None:
    async with Client(server_with()) as client:
        result = await client.read_resource("event://run:341")
    content = result.contents[0]
    assert content.mime_type == "application/json"
    assert json.loads(content.text)["event"]["event_id"] == CRISIS.key


async def test_event_resource_by_number_and_unknown_event() -> None:
    async with Client(server_with()) as client:
        found = await client.read_resource("event://407")
        with pytest.raises(MCPError, match="no event 'run:999'"):
            await client.read_resource("event://run:999")
    assert json.loads(found.contents[0].text)["event"]["event_id"] == "run:407"


async def test_event_resource_when_the_graph_is_down() -> None:
    async with Client(server_with(graph=DownGraph())) as client:
        with pytest.raises(MCPError, match="not reachable"):
            await client.read_resource("event://run:341")


async def test_daily_briefing_prompt_names_the_graph_time_and_the_tools() -> None:
    async with Client(server_with()) as client:
        prompt = await client.get_prompt("daily_briefing")
    text = prompt.messages[0].content.text
    assert prompt.messages[0].role == "user"
    assert "2026-11-05 22:54 UTC" in text
    assert "get_trending_events" in text and "get_event" in text


async def test_daily_briefing_prompt_works_without_the_graph() -> None:
    async with Client(server_with(graph=DownGraph())) as client:
        prompt = await client.get_prompt("daily_briefing")
    assert "get_trending_events" in prompt.messages[0].content.text


async def test_explain_event_prompt_embeds_the_event() -> None:
    async with Client(server_with()) as client:
        prompt = await client.get_prompt("explain_event", {"event_id": "341"})
    text = prompt.messages[0].content.text
    assert f"Explain the news event {CRISIS.key}" in text
    assert '"title": "Paraguayan president resigns after Ueno Bank collapse"' in text


async def test_explain_event_prompt_with_an_unknown_event() -> None:
    async with Client(server_with()) as client:
        with pytest.raises(MCPError, match="no event"):
            await client.get_prompt("explain_event", {"event_id": "run:999"})


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "127.0.0.2"])
def test_loopback_hosts_are_accepted(host: str) -> None:
    assert check_host(host) == host


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com"])
def test_other_hosts_are_refused(host: str) -> None:
    with pytest.raises(SystemExit, match="Refusing"):
        check_host(host)

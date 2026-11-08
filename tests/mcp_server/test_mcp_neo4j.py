"""Integration tests: the MCP server on the frozen graph snapshot, read only.

Needs the `neo4j-eval` service (`docker compose --profile eval up -d`, filled by
`scripts/snapshot_graph.py restore`); skipped when it isn't reachable. Writes nothing.
Also starts the real server as a subprocess, over stdio and over HTTP, the way Claude
Desktop and other clients do.
"""

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import anyio
import pytest
from mcp import Client, StdioServerParameters
from neo4j.exceptions import AuthError, ServiceUnavailable

from tests.mcp_server.test_server import TOOLS
from wikipulse.config import get_settings
from wikipulse.mcp_server.explore import Explorer
from wikipulse.mcp_server.server import build_server
from wikipulse.qa.graph import QAGraph

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
TIMEOUT_S = 90  # the subprocess imports the QA stack, which takes a while


@pytest.fixture(scope="module")
def snapshot_uri() -> str:
    uri = get_settings().eval_qa_neo4j_uri
    graph = QAGraph.from_settings(get_settings().model_copy(update={"neo4j_uri": uri}))
    try:
        graph.driver.verify_connectivity()
    except (ServiceUnavailable, AuthError, OSError):
        pytest.skip("the snapshot server (neo4j-eval) is not running")
    finally:
        graph.close()
    return uri


@pytest.fixture
def graph(snapshot_uri: str) -> Iterator[QAGraph]:
    graph = QAGraph.from_settings(get_settings().model_copy(update={"neo4j_uri": snapshot_uri}))
    yield graph
    graph.close()


def test_the_new_reads_of_qa_graph(graph: QAGraph) -> None:
    now = graph.clock()
    keys = graph.trending_events(now - timedelta(hours=24), limit=5)
    assert len(keys) == 5
    events = {e.key: e for e in graph.events(keys)}
    sizes = [events[k].page_count for k in keys]
    assert sizes == sorted(sizes, reverse=True)
    assert all(events[k].status in ("active", "ended") and events[k].score for k in keys)
    assert graph.event_briefing(keys[0]) is None  # no briefings before spec 05d
    entity = graph.event_entities(keys[0], limit=1)[0]
    assert [e.key for e in graph.entities_by_key([entity.entity_key])] == [entity.entity_key]
    assert graph.entity_event_counts([entity.entity_key])[entity.entity_key] >= 1


async def test_the_tools_on_the_snapshot(graph: QAGraph) -> None:
    server = build_server(Explorer(graph))
    async with Client(server) as client:
        trending = (await client.call_tool("get_trending_events", {"limit": 3})).structured_content
        top = trending["events"][0]["event_id"]
        event = (await client.call_tool("get_event", {"event_id": top})).structured_content
        a, b = event["entities"][0], event["entities"][1]
        connection = await client.call_tool(
            "find_connection", {"entity_a": a["entity_id"], "entity_b": b["entity_id"]}
        )
        search = await client.call_tool("search_entities", {"name": a["name"]})
        resource = await client.read_resource(f"event://{top}")
    assert len(trending["events"]) == 3 and trending["as_of"]
    assert event["event"]["event_id"] == top and len(event["entities"]) >= 2
    urls = [e["source"]["url"] for e in event["entities"]]
    assert all(url.startswith("https://en.wikipedia.org/") for url in urls)
    # Two entities of the same event are at most two steps apart (via the event).
    paths = connection.structured_content["paths"]
    assert paths and len(paths[0]["steps"]) <= 2
    assert search.structured_content["entities"][0]["entity_id"] == a["entity_id"]
    assert top in resource.contents[0].text


async def test_the_server_over_stdio(snapshot_uri: str) -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "wikipulse.mcp_server.server", "--neo4j-uri", snapshot_uri],
        cwd=ROOT,
    )
    with anyio.fail_after(TIMEOUT_S):
        async with Client(parameters) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            result = await client.call_tool("get_trending_events", {"limit": 2})
    assert names == TOOLS
    assert not result.is_error and len(result.structured_content["events"]) == 2


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_for_port(port: int, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"the server stopped with exit code {process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.5)
    raise TimeoutError(f"the server did not listen on port {port}")


async def test_the_server_over_http(snapshot_uri: str) -> None:
    port = _free_port()
    command = [sys.executable, "-m", "wikipulse.mcp_server.server", "--transport", "http"]
    command += ["--port", str(port), "--neo4j-uri", snapshot_uri]
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "LOG_LEVEL": "WARNING"},
    )
    try:
        await anyio.to_thread.run_sync(_wait_for_port, port, process)
        with anyio.fail_after(TIMEOUT_S):
            async with Client(f"http://127.0.0.1:{port}/mcp") as client:
                names = {t.name for t in (await client.list_tools()).tools}
                result = await client.call_tool("search_entities", {"name": "Paraguay"})
    finally:
        process.terminate()
        process.wait(timeout=30)
    assert names == TOOLS
    assert result.structured_content["entities"][0]["name"] == "Paraguay"


def _rpc_lines(process: subprocess.Popen, out: queue.Queue[str]) -> None:
    for line in process.stdout:
        out.put(line)


def test_an_older_client_lists_everything_over_raw_json_rpc(snapshot_uri: str) -> None:
    """What the MCP Inspector and Claude Desktop do: an `initialize` handshake with an
    older protocol version, then the four list requests, as plain JSON-RPC lines."""
    process = subprocess.Popen(
        [sys.executable, "-m", "wikipulse.mcp_server.server", "--neo4j-uri", snapshot_uri],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=_rpc_lines, args=(process, lines), daemon=True).start()

    def request(number: int, method: str, params: dict) -> dict:
        message = {"jsonrpc": "2.0", "id": number, "method": method, "params": params}
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
        return json.loads(lines.get(timeout=TIMEOUT_S))["result"]

    try:
        hello = request(
            1,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "raw-test", "version": "0"},
            },
        )
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        process.stdin.write("\n")
        tools = request(2, "tools/list", {})["tools"]
        templates = request(3, "resources/templates/list", {})["resourceTemplates"]
        prompts = request(4, "prompts/list", {})["prompts"]
    finally:
        process.stdin.close()  # end of input: the server exits
        process.wait(timeout=30)
    assert hello["protocolVersion"] == "2025-06-18"
    assert hello["serverInfo"]["name"] == "wikipulse"
    assert {t["name"] for t in tools} == TOOLS
    assert all(t["annotations"]["readOnlyHint"] is True for t in tools)
    assert [t["uriTemplate"] for t in templates] == ["event://{event_id}"]
    assert {p["name"] for p in prompts} == {"daily_briefing", "explain_event"}
    assert process.returncode == 0


@pytest.mark.live
async def test_ask_with_the_local_model(snapshot_uri: str) -> None:
    """The real QA pipeline behind the `ask` tool (needs Ollama with QA_MODEL)."""
    settings = get_settings().model_copy(update={"neo4j_uri": snapshot_uri})
    explorer = Explorer.from_settings(settings)
    try:
        async with Client(build_server(explorer)) as client:
            question = "How are Ueno Bank and Santiago Peña connected?"
            answered = await client.call_tool("ask", {"question": question})
            refused = await client.call_tool(
                "ask", {"question": "What did the Federal Reserve decide about interest rates?"}
            )
    finally:
        explorer.close()
    data = answered.structured_content
    assert data["answerable"] and "[1]" in data["answer"]
    assert data["sources"][0]["url"].startswith("https://en.wikipedia.org/w/index.php?oldid=")
    assert refused.structured_content["answerable"] is False

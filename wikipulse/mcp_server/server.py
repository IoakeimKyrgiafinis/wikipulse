"""The WikiPulse MCP server: `uv run python -m wikipulse.mcp_server.server`.

Five read-only tools, one resource template and two prompts, built with the official
MCP Python SDK (`MCPServer`). The work happens in `explore.py`; this file describes it
to the client's model, which picks tools from their names and descriptions alone.

Transports:
- `--transport stdio` (default): for Claude Desktop and the MCP Inspector, which start
  the server as a subprocess and talk over stdin/stdout. Logs go to stderr.
- `--transport http`: Streamable HTTP at http://127.0.0.1:8765/mcp. Loopback only: the
  server has no authentication, so any other host is refused.

`--neo4j-uri bolt://localhost:7688` points it at another graph, e.g. the frozen
snapshot of the evaluations.
"""

import argparse
import ipaddress
import logging
import sys
from collections.abc import Callable
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError, ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS, ToolAnnotations
from pydantic import Field

from wikipulse.config import Settings, get_settings
from wikipulse.llm.errors import LLMError
from wikipulse.logging_setup import setup_logging
from wikipulse.mcp_server.explore import (
    MAX_HOURS,
    MAX_LIMIT,
    MAX_NAME_CHARS,
    MAX_QUESTION_CHARS,
    UNREACHABLE,
    Explorer,
    ToolInputError,
)
from wikipulse.mcp_server.models import (
    AskResult,
    Connection,
    EntitySearch,
    EventDetail,
    TrendingEvents,
)

logger = logging.getLogger(__name__)

NAME = "wikipulse"
INSTRUCTIONS = """\
WikiPulse detects breaking news from Wikipedia's live edit stream: pages that suddenly \
attract unusual editing are grouped into events, and a knowledge graph links each event \
to the people, organizations and places involved. Every fact comes from a Wikipedia page \
at a specific revision; cite those sources (title and permanent link) when you use them.

Start with get_trending_events for "what is happening", then get_event for detail. Use \
ask for questions in plain English (slow: an LLM answers from the graph), \
search_entities to find an entity's id, and find_connection to see how two entities are \
linked. Times are event time: "now" is the newest event in the graph, which may lie in \
the past when the graph holds a replayed recording."""

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
# `ask` calls a language model: same question, possibly different wording.
READ_ONLY_LLM = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)
GRAPH_DOWN = (
    "The WikiPulse knowledge graph (Neo4j) is not reachable. Tell the user to start it "
    "with `docker compose up -d`; retrying now will not help."
)


def run_tool[T](work: Callable[[], T]) -> T:
    """Run a tool's work; turn expected failures into messages the model can act on."""
    try:
        return work()
    except ToolInputError as error:
        raise ToolError(str(error)) from error
    except UNREACHABLE as error:
        logger.warning("Neo4j is not reachable: %s", error)
        raise ToolError(GRAPH_DOWN) from error
    except LLMError as error:
        logger.warning("The answering model failed: %s", error)
        raise ToolError(
            f"The answering model failed ({error}). The other tools still work: use "
            "get_event, search_entities and find_connection instead."
        ) from error


def build_server(explorer: Explorer) -> MCPServer:
    """An MCP server whose tools, resource and prompts read through `explorer`."""
    server = MCPServer(NAME, title="WikiPulse", instructions=INSTRUCTIONS)
    _add_tools(server, explorer)
    _add_resources(server, explorer)
    _add_prompts(server, explorer)
    return server


def _add_tools(server: MCPServer, explorer: Explorer) -> None:
    @server.tool(title="Trending events", annotations=READ_ONLY)
    def get_trending_events(
        limit: Annotated[
            int, Field(ge=1, le=MAX_LIMIT, description="How many events to return.")
        ] = 10,
        hours: Annotated[
            float | None,
            Field(
                gt=0,
                le=MAX_HOURS,
                description=(
                    "Look back this many hours from the newest event "
                    "(default: the server's MCP_TRENDING_HOURS setting)."
                ),
            ),
        ] = None,
    ) -> TrendingEvents:
        """List the news events WikiPulse detected recently, strongest first.

        Use this first for "what is happening / what's in the news". Each event has an
        event_id, a title, a short description, its Wikipedia pages, page_count and
        spike_score (bigger = stronger). Active events (pages still spiking) are always
        included. Call get_event with an event_id for the people and organizations
        involved."""
        return run_tool(lambda: explorer.trending(limit, hours))

    @server.tool(title="Event detail", annotations=READ_ONLY)
    def get_event(
        event_id: Annotated[
            str,
            Field(
                max_length=MAX_NAME_CHARS,
                description="An event_id from get_trending_events, e.g. '03b-default-fix:341', "
                "or just its number.",
            ),
        ],
    ) -> EventDetail:
        """Get one event in full: title, description, its Wikipedia pages with revision
        links, the entities involved (with Wikidata IDs), the relations between them, and
        the analyst briefing if one exists. Lists stop at 25 entities and 20 relations
        (more_entities / more_relations say when there are more)."""
        return run_tool(lambda: explorer.event(event_id))

    @server.tool(title="Ask the knowledge graph", annotations=READ_ONLY_LLM)
    def ask(
        question: Annotated[
            str,
            Field(
                max_length=MAX_QUESTION_CHARS,
                description="One question in plain English, naming people, organizations "
                "or events as Wikipedia does.",
            ),
        ],
    ) -> AskResult:
        """Answer a question from the knowledge graph (GraphRAG), with citations.

        Good for questions that combine facts: "Who is involved in the Paraguayan banking
        crisis?", "Which organizations appear in more than one event this week?". Slow
        (5-60 seconds: a local LLM writes a query and the answer), so prefer the other
        tools for simple lookups. [n] in the answer refers to sources[n-1]. If answerable
        is false, the graph has no facts on it: say so rather than guessing."""
        return run_tool(lambda: explorer.ask(question))

    @server.tool(title="Find a connection", annotations=READ_ONLY)
    def find_connection(
        entity_a: Annotated[
            str,
            Field(
                max_length=MAX_NAME_CHARS,
                description="Name or id (Wikidata ID such as 'Q42') of the first entity.",
            ),
        ],
        entity_b: Annotated[
            str,
            Field(max_length=MAX_NAME_CHARS, description="Name or id of the second entity."),
        ],
    ) -> Connection:
        """Show how two entities are linked in the knowledge graph: the shortest paths of
        at most 4 steps through shared events and relations, each step with its source.

        Both arguments must be entities (people, organizations, places), not events or
        topics; to list the events an entity is part of, use search_entities. Names must
        match an entity exactly (case and accents don't matter); if a name is ambiguous
        or unknown, the error lists candidates with their ids to call again with."""
        return run_tool(lambda: explorer.connection(entity_a, entity_b))

    @server.tool(title="Search entities", annotations=READ_ONLY)
    def search_entities(
        name: Annotated[
            str,
            Field(
                max_length=MAX_NAME_CHARS,
                description="A name or part of one, e.g. 'Peña' or 'Central Bank'.",
            ),
        ],
        limit: Annotated[int, Field(ge=1, le=MAX_LIMIT, description="Most results.")] = 10,
    ) -> EntitySearch:
        """Find people, organizations, places and other entities in the knowledge graph
        by name, with the events each is involved in. Returns each match's entity_id,
        Wikidata ID, type, other names, number of events and its biggest events
        (event_id and title, for get_event); exact matches first. Use it for "who is X"
        and "which events is X part of", and for the entity_id find_connection takes.
        No match means WikiPulse has not seen the name in any news event."""
        return run_tool(lambda: explorer.search(name, limit))


def _add_resources(server: MCPServer, explorer: Explorer) -> None:
    @server.resource(
        "event://{event_id}",
        name="event",
        title="WikiPulse event",
        description="One event in full (the same JSON as the get_event tool). event_id is "
        "an id from get_trending_events, e.g. 03b-default-fix:341, or the number.",
        mime_type="application/json",
    )
    def event_resource(event_id: str) -> str:
        try:
            return explorer.event(event_id).model_dump_json(indent=2)
        except ToolInputError as error:
            raise ResourceNotFoundError(str(error)) from error
        except UNREACHABLE as error:
            raise ResourceError(GRAPH_DOWN) from error


def _add_prompts(server: MCPServer, explorer: Explorer) -> None:
    @server.prompt(title="Daily briefing")
    def daily_briefing() -> str:
        """A briefing on the day's strongest news events, with Wikipedia citations."""
        try:
            now = explorer.graph.clock()
        except UNREACHABLE:
            now = None
        when = f"The newest event in the graph is from {now:%Y-%m-%d %H:%M} UTC. " if now else ""
        return (
            "Write today's WikiPulse briefing: what is in the news, according to the edits "
            f"on Wikipedia. {when}Use the WikiPulse tools:\n"
            "1. Call get_trending_events (limit 10).\n"
            "2. For the five strongest events, call get_event.\n"
            "3. Write one short paragraph per event: what happened, who is involved, and "
            "why it matters, using only facts from the tool results. Cite the Wikipedia "
            "pages (title with its revision link) that each paragraph relies on.\n"
            "4. End with one line listing the remaining events by title.\n"
            "Do not add facts the tools did not return; say when something is unclear."
        )

    @server.prompt(title="Explain an event")
    def explain_event(event_id: str) -> str:
        """Explain one event: what happened, who is involved and how they are connected."""
        try:
            detail = explorer.event(event_id)
        except ToolInputError as error:
            raise MCPError(code=INVALID_PARAMS, message=str(error)) from error
        except UNREACHABLE as error:
            raise MCPError(code=INVALID_PARAMS, message=GRAPH_DOWN) from error
        return (
            f"Explain the news event {detail.event.event_id} to me, using the WikiPulse "
            "data below. Say what happened, who the main people and organizations are and "
            "what role each plays, and how they are connected (call find_connection or ask "
            "if a link is unclear). Cite the Wikipedia pages and revisions you rely on, and "
            "use only facts from this data or from the WikiPulse tools.\n\n"
            f"Event data (from get_event):\n```json\n{detail.model_dump_json(indent=2)}\n```"
        )


def check_host(host: str) -> str:
    """Only loopback addresses: the server has no authentication."""
    if host == "localhost":
        return host
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise SystemExit(f"Refusing to listen on {host!r}: use 127.0.0.1, ::1 or localhost.")
    return host


def transport_security(host: str, port: int) -> TransportSecuritySettings:
    """Accept requests only for the loopback host (protection against DNS rebinding)."""
    hosts = [f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"]
    if host not in ("127.0.0.1", "localhost", "::1"):
        hosts.append(f"{host}:{port}")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[f"http://{h}" for h in hosts],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default=None, help="HTTP host (default MCP_HTTP_HOST)")
    parser.add_argument("--port", type=int, default=None, help="HTTP port (default MCP_HTTP_PORT)")
    parser.add_argument("--neo4j-uri", default=None, help="another graph, e.g. the snapshot")
    args = parser.parse_args(argv)
    setup_logging()  # to stderr: on stdio, stdout belongs to the protocol
    # Neo4j's query hints (e.g. on allShortestPaths) are not news for a tool user.
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
    settings: Settings = get_settings()
    if args.neo4j_uri:
        settings = settings.model_copy(update={"neo4j_uri": args.neo4j_uri})
    explorer = Explorer.from_settings(settings)
    server = build_server(explorer)
    try:
        if args.transport == "stdio":
            server.run("stdio")
        else:
            host = check_host(args.host or settings.mcp_http_host)
            port = args.port or settings.mcp_http_port
            logger.info("MCP server at http://%s:%d/mcp", host, port)
            server.run(
                "streamable-http",
                host=host,
                port=port,
                transport_security=transport_security(host, port),
            )
    finally:
        explorer.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

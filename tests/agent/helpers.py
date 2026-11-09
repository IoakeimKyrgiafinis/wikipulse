"""Fakes for the agent tests: a scripted LLM, a fake MCP session, a tiny event graph.
No Neo4j, no Ollama, no network."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import anyio
from mcp import Client

from wikipulse.agent.fixtures import FixtureEvent, FixtureGraph
from wikipulse.agent.loop import AgentLimits, AgentRun, AnalystAgent
from wikipulse.agent.tools import ToolBox, ToolLimits
from wikipulse.llm.client import ChatRequest, LLMClient
from wikipulse.llm.errors import LLMUnavailableError
from wikipulse.llm.types import LLMResponse, ToolCall, Usage
from wikipulse.mcp_server.explore import Explorer
from wikipulse.mcp_server.server import build_server

Answer = LLMResponse | str | Callable[[ChatRequest], LLMResponse | str] | Exception

EVENT = FixtureEvent.model_validate(
    {
        "key": "run:7",
        "title": "Ueno Bank collapses",
        "description": "Ueno Bank collapsed and the president resigned.",
        "started_at": "2026-11-05T16:00:00Z",
        "ended_at": "2026-11-05T18:00:00Z",
        "pages": [
            {
                "title": "Ueno Bank",
                "revision": 101,
                "score": 9.0,
                "description": "Paraguayan bank",
                "summary": "Ueno Bank was a Paraguayan bank. It collapsed in November 2026.",
            },
            {
                "title": "Santiago Peña",
                "revision": 102,
                "score": 5.0,
                "summary": "Santiago Peña is a Paraguayan politician.",
            },
        ],
        "entities": [
            {"key": "Q1", "name": "Ueno Bank", "type": "organization", "page": "Ueno Bank"},
            {"key": "Q2", "name": "Santiago Peña", "type": "person", "page": "Santiago Peña"},
        ],
        "relations": [
            {
                "subject": "Santiago Peña",
                "relation": "resigned as president of",
                "object": "Paraguay",
                "page": "Santiago Peña",
            }
        ],
    }
)
OTHER = FixtureEvent.model_validate(
    {
        "key": "run:9",
        "title": "Peña's successor sworn in",
        "started_at": "2026-11-06T10:00:00Z",
        "pages": [{"title": "Pedro Alliana", "revision": 301, "summary": "A politician."}],
        "entities": [
            {"key": "Q2", "name": "Santiago Peña", "type": "person", "page": "Pedro Alliana"}
        ],
    }
)


def response(text: str = "", calls: list[ToolCall] | None = None, tokens: int = 100) -> LLMResponse:
    usage = Usage(input_tokens=tokens, output_tokens=10)
    return LLMResponse(model="fake:m", text=text, tool_calls=calls or [], usage=usage)


def call(tool: str, n: int = 0, /, **arguments: Any) -> ToolCall:
    return ToolCall(id=f"call_{n}", name=tool, arguments=arguments)


class ScriptedLLM(LLMClient):
    """Answers from a list: a response, a text, a function of the request, or an error."""

    backend = "fake"

    def __init__(self, answers: list[Answer]) -> None:
        super().__init__("m")
        self.answers = list(answers)
        self.requests: list[ChatRequest] = []

    def _send(self, request: ChatRequest) -> LLMResponse:
        self.requests.append(request)
        if not self.answers:
            raise AssertionError("the scripted LLM ran out of answers")
        answer = self.answers.pop(0)
        if callable(answer) and not isinstance(answer, LLMResponse | Exception):
            answer = answer(request)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, str):
            answer = response(answer)
        return answer.model_copy(deep=True)


DOWN = LLMUnavailableError("Ollama is not reachable")


def last_tool_text(request: ChatRequest) -> str:
    return next(m.content for m in reversed(request.messages) if m.role == "tool")


@dataclass
class Listed:
    name: str
    description: str = "A tool."
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})


@dataclass
class Result:
    content: list[Any]
    structured_content: dict[str, Any] | None = None
    is_error: bool = False


@dataclass
class Text:
    text: str


DETAIL = {
    "event": {"event_id": "run:7", "title": "Ueno Bank collapses", "page_count": 1},
    "pages": [{"title": "Ueno Bank", "revision": 101, "url": "u"}],
    "entities": [],
    "relations": [],
}


class FakeMcp:
    """An MCP session whose tools return prepared results, some after a delay."""

    names: tuple[str, ...] = ("get_event", "ask", "find_connection", "search_entities")

    def __init__(
        self, results: dict[str, Result] | None = None, delays: dict[str, float] | None = None
    ) -> None:
        self.results = {"get_event": Result([Text("{}")], DETAIL), **(results or {})}
        self.delays = delays or {}
        self.calls: list[tuple[str, dict[str, Any], Any]] = []

    async def list_tools(self) -> Any:
        @dataclass
        class Listing:
            tools: list[Listed]

        return Listing([Listed(name) for name in self.names])

    async def call_tool(self, name: str, arguments=None, read_timeout_seconds=None) -> Result:
        self.calls.append((name, arguments or {}, read_timeout_seconds))
        if self.delays.get(name):
            await anyio.sleep(self.delays[name])
        return self.results.get(name, Result([Text(f"{name} result")]))


def fixture_graph(*events: FixtureEvent) -> FixtureGraph:
    return FixtureGraph.of(list(events or (EVENT, OTHER)))


def build_agent(llm: ScriptedLLM, session: Any, graph: FixtureGraph, **limits: Any) -> AnalystAgent:
    tool_limits = limits.pop("tool_limits", None)
    verify = limits.pop("verify", False)  # the grounding check has its own tests
    tools = ToolBox(session, graph, tool_limits or ToolLimits())
    return AnalystAgent(llm, tools, AgentLimits(**limits), verify=verify)


async def run_agent(
    answers: list[Answer], event: str = "run:7", graph: FixtureGraph | None = None, **limits: Any
) -> tuple[AgentRun, ScriptedLLM]:
    """One run over the real MCP server code (in-process) on the fixture graph."""
    graph = graph or fixture_graph()
    llm = ScriptedLLM(answers)
    async with Client(build_server(Explorer(graph))) as session:
        run = await build_agent(llm, session, graph, **limits).run(event)
    return run, llm

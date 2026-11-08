# Spec 05c: MCP server

Status: done
Stage: 5. GraphRAG, MCP, agent
Owner: agent
Depends on: 05a

## Goal
Expose WikiPulse through MCP, so Claude Desktop or any MCP client can ask what's happening and explore the graph.

## What you'll learn
- **The MCP protocol:** tools, resources, prompts and transports, by testing with the MCP Inspector.
- **Tool design as prompt design:** the client model decides what to call from names and descriptions alone. You'll rewrite descriptions after watching Claude misuse them.
- **API design for AI consumers:** output size limits and error messages that tell the model what to do next.

## Interface (a starting point; improve it if you find a better design)
Tools, all read-only:
- `get_trending_events(limit=10)`: active events with titles, pages and strength
- `get_event(event_id)`: full detail with entities and a briefing if one exists
- `ask(question)`: GraphRAG answer with citations
- `find_connection(entity_a, entity_b)`: shortest paths between two entities
- `search_entities(name)`: matching entities with Wikidata IDs

Resources: `event://{event_id}`. Prompts: `daily_briefing`, `explain_event(event_id)`.

## Outputs
- `wikipulse/mcp_server/server.py` (official MCP Python SDK), runnable over stdio and HTTP (localhost only)
- Tests using the SDK's client in-process; README section with the Claude Desktop configuration

## Requirements
1. Tools reuse existing modules; no duplicated logic.
2. Output limits and helpful error messages.
3. Tools marked read-only with the SDK's annotations.

## Acceptance criteria
- In-process tests for every tool, resource and error case.
- The MCP Inspector lists everything.

## Look at it yourself (optional, never blocking)
- Connect Claude Desktop, ask 10 questions, watch the tool calls, and improve descriptions where it struggled.

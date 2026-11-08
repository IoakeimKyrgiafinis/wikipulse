# Learning note: 05c MCP server

## What was built

WikiPulse now speaks MCP (the Model Context Protocol), so any MCP client (Claude
Desktop, the MCP Inspector, the analyst agent of spec 05d) can ask what is happening and
explore the knowledge graph without knowing anything about Neo4j or Cypher:

```
uv run python -m wikipulse.mcp_server.server                    # stdio
uv run python -m wikipulse.mcp_server.server --transport http   # http://127.0.0.1:8765/mcp
```

| File | What it does |
|------|--------------|
| `wikipulse/mcp_server/explore.py` | `Explorer`: the work behind every tool as plain methods (`trending()`, `event()`, `ask()`, `connection()`, `search()`), plus `find_event()` and `resolve_entity()` that turn what a model typed into a graph id, the output limits, and `ToolInputError` (a request that can't be served, with a message saying what to do instead). `LazyAsker` holds one `QASession` for `ask`, created at the first question. |
| `wikipulse/mcp_server/models.py` | The pydantic models the tools return (`TrendingEvents`, `EventDetail`, `AskResult`, `Connection`, `EntitySearch`, ...). They become each tool's output schema. |
| `wikipulse/mcp_server/server.py` | `build_server()`: the official MCP Python SDK's `MCPServer` with five tools, the resource template `event://{event_id}` and two prompts; `run_tool()` maps failures to tool errors; `main()` with `--transport stdio|http`, `--host`, `--port`, `--neo4j-uri`. |
| `wikipulse/qa/graph.py` | Four new read-only queries on `QAGraph` (`trending_events()`, `event_briefing()`, `entities_by_key()`, `entity_event_counts()`), and `EventInfo` now carries `status`, `ended_at` and `score`. |
| `wikipulse/config.py`, `.env.example` | `MCP_HTTP_HOST`, `MCP_HTTP_PORT`, `MCP_TRENDING_HOURS`. |
| `docs/claude_desktop_config.example.json`, `README.md` | How to connect Claude Desktop, and the commands. |
| `tests/mcp_server/` | 61 unit tests (a fake graph, a scripted answerer, the SDK's in-process client) and 5 integration tests on the frozen snapshot, including the real server as a subprocess over stdio, over HTTP, and spoken to in raw JSON-RPC. One `live` test runs `ask` with the local model. |

How a tool call flows:

```
MCP client (Claude Desktop, Inspector, agent)
  │  JSON-RPC over stdio, or Streamable HTTP on 127.0.0.1
  ▼
MCPServer (server.py)        validates arguments against the input schema (limits!)
  │  tool function → run_tool(lambda: explorer.xxx(...))   (sync, run in a worker thread)
  ▼
Explorer (explore.py)        resolves ids and names, applies output limits
  │
  ├─ QAGraph (wikipulse.qa.graph)   every read in a Neo4j READ session
  └─ QASession.ask (wikipulse.qa)   only for `ask`: retrieval + cited answer by the LLM
  ▼
pydantic model ──► structured content + the same JSON as text
errors ──► ToolInputError / Neo4j down / LLM down ──► is_error result with a next step
```

| Tool | Reuses | Returns |
|------|--------|---------|
| `get_trending_events(limit=10, hours=None)` | `QAGraph.clock()`, `trending_events()`, `events()` | active events and those started in the last 24 h of event time, most pages first |
| `get_event(event_id)` | `events()`, `events_by_number()`, `event_entities()`, `event_relations()`, `event_briefing()` | pages with revision links, entities with Wikidata IDs and sources, relations, briefing |
| `ask(question)` | `QASession.ask()` (05a) | the cited answer, sources numbered like the `[n]` markers, notes |
| `find_connection(entity_a, entity_b)` | `search_entities()`, `fold()`, `lucene_terms()`, `paths()`, `path_text()` | up to 3 shortest paths of at most 4 steps, each step with its source |
| `search_entities(name, limit=10)` | `search_entities()`, `entity_event_counts()` | matches with entity id, Wikidata ID, type, aliases, number of events |

Resource template `event://{event_id}` returns the same JSON as `get_event`. Prompt
`daily_briefing` tells the client model how to write a cited briefing with the tools (and
what "today" is in the graph); `explain_event(event_id)` embeds the event's data and asks
for an explanation.

## Key concepts

### The MCP protocol: tools, resources, prompts and transports

MCP is a small JSON-RPC protocol between a *client* (an app that runs a language model,
such as Claude Desktop) and a *server* (a program that offers capabilities, here
WikiPulse). The client asks the server what it offers, shows that to its model, and
carries out what the model asks for. Three kinds of capability:

- **Tools** are functions the *model* decides to call (`tools/list`, `tools/call`). Each
  has a name, a description, an input schema (JSON Schema of its arguments) and, here,
  an output schema. `server.py` declares them with `@server.tool(...)` on ordinary
  Python functions; the SDK reads the type hints (`Annotated[int, Field(ge=1, le=25)]`)
  to build the input schema and validates every call against it before our code runs.
  Returning a pydantic model (`TrendingEvents`) makes the SDK publish its schema as the
  output schema and send the result twice: as structured JSON (`structuredContent`)
  and as the same JSON in a text block, for clients that only read text.
- **Resources** are data the *user or app* chooses to attach (`resources/read`), named
  by URIs. `event://{event_id}` is a resource *template*: one URI pattern for all
  events. `event_resource()` returns the same JSON as `get_event`. The difference from
  the tool is who decides: a tool call is the model's choice in the middle of a
  conversation, a resource is something the user picks ("attach event 341").
- **Prompts** are message templates the *user* picks (`prompts/get`), usually shown as
  slash commands. `daily_briefing` and `explain_event` return a user message that tells
  the client model which tools to call and how to cite; `explain_event` already
  contains the event's data, so the model starts with the facts.

**Transports** carry the JSON-RPC messages:

- **stdio**: the client starts the server as a subprocess and writes one JSON message
  per line to its stdin, reading answers from its stdout. This is how Claude Desktop and
  the MCP Inspector run local servers. Consequence: *nothing else may print to stdout*.
  `setup_logging()` logs to stderr, and the server exits cleanly when stdin closes
  (`test_an_older_client_lists_everything_over_raw_json_rpc` checks exit code 0).
- **Streamable HTTP**: the server listens on a port and the client POSTs messages to
  `/mcp`. Useful for several clients at once or a server in another process. WikiPulse
  has no authentication, so `check_host()` refuses anything but loopback addresses, and
  `transport_security()` turns on the SDK's DNS-rebinding protection (a web page in
  your browser can't talk to the server by pointing a domain name at 127.0.0.1).

A session starts with a handshake: the client sends `initialize` with the protocol
version it speaks (e.g. `2025-06-18`), the server answers with the version it will use,
its capabilities (tools, resources, prompts), its name and its `instructions`, a short
text the client may give its model as background. `INSTRUCTIONS` in `server.py`
explains what WikiPulse is, which tool to start with, that facts carry Wikipedia
revisions to cite, and that "now" is event time.

The SDK installed is `mcp` 2.3.0, a newer major version than most tutorials show:
`FastMCP` is now `MCPServer` (`mcp.server.mcpserver`), and `mcp.Client` connects to a
server object in-process, to a command over stdio (`StdioServerParameters`) or to a URL.
It also speaks a newer protocol revision (2026-07-28) without the handshake, and still
accepts the older handshake that Claude Desktop and the Inspector use; the raw JSON-RPC
test checks exactly that.

### Tool design is prompt design

The client model never sees our code. It picks a tool, and fills in the arguments, from
the tool's name, description and input schema alone. So these are prompts, and they
were written and revised like prompts:

- **Say when to use it, not only what it does.** `get_trending_events` starts "Use this
  first for 'what is happening'"; `ask` says it is slow and what kind of question it is
  good for, so a model doesn't spend 30 seconds on a name lookup.
- **Say what the output means.** `spike_score` "(bigger = stronger)", `answerable` "False
  when the graph has no facts to answer it", "[n] in the answer refers to
  sources[n-1]". Field descriptions in `models.py` travel with the output schema.
- **Point to the next tool.** `event_id`: "Pass this to get_event or read
  event://{event_id}"; `search_entities` returns `top_events` with ids for `get_event`.
- **Accept what models naturally type.** `find_event()` takes "03b-default-fix:341",
  "341", "event 341", "#341" and "event://..."; `resolve_entity()` takes a Wikidata ID,
  a name in any case, with or without accents ("Santiago Pena"), or an alias.

What the descriptions were tested against: Claude Desktop (the spec's optional check)
is the human's and was not available to me, so I used the closest thing that runs here,
`qwen3:8b` as the client model. A throwaway script (not committed; results in
`data/mcp/routing-v1.jsonl` to `-v3.jsonl`) lists the tools through the SDK client,
turns them into `wikipulse.llm` `ToolSpec`s with `INSTRUCTIONS` as the system prompt,
and runs ten user questions with up to four tool rounds each against the snapshot,
recording every call. What went wrong, and what changed:

| Question | Run 1 (first descriptions) | Change | Afterwards |
|---|---|---|---|
| "Who is Pedro Alliana and which events is he part of?" | `search_entities`, then `find_connection("Q51885479", "event")` (an error), then `ask` | No tool listed an entity's events, so the model bent `find_connection` to it. `search_entities` now returns each match's `top_events` (id and title, from the existing `QAGraph.entity_events()`), and the descriptions say which tool lists events and that `find_connection` takes entities only. | run 2 and 3: `search_entities`, then `get_event` on the right event, no error |
| "Is there anything about the Federal Reserve?" | `search_entities` found nothing; the model then offered to "provide information about the Federal Reserve" from its own knowledge | The empty-result note now says WikiPulse has not seen the name, and not to answer from other knowledge as if it came from WikiPulse. | run 2: the search now matched "East Atlantic Biosphere Reserve Network" (shares the word "Reserve") and the model mentioned it as related |
| (same) | | A note when no match contains *every* word of the query: "these share only some words and are probably something else". | run 3: "no direct mention ... the East Atlantic Biosphere Reserve Network ... is unrelated" |
| "Find people called Paxton." | names Ken Paxton, "involved in 1 event" | (`top_events`) | names the event and its id |

The other questions picked the expected tool on the first try in all three runs:
`get_trending_events` for "what's in the news" and "what happened in sports today",
`find_connection` for both "how are X and Y connected", `get_event("341")` for "explain
event 341", `ask` for "tell me more about the Paraguayan banking crisis" and the
aggregation question. The sports question is answered only from the five strongest
events (it found the SAFF Championship among them); `ask` would have searched all.

### API design for AI consumers: output limits and errors that say what to do next

A tool result goes into the client model's context window, which is limited and costs
time. And a model can't read a stack trace and go fix the code; it can only call again
differently. Both shape the API:

- **Every list has a cap, and says when it was cut.** `get_trending_events` and
  `search_entities` return at most 25 items (enforced by the input schema, so
  `limit=300` is rejected with "Input should be less than or equal to 25" before any
  query runs); `get_event` at most 10 pages, 25 entities and 20 relations, with
  `more_entities`/`more_relations` set when there were more (the query asks for one
  more than it shows, the cheap way to know); descriptions are cut at 400 characters;
  `ask` takes at most 500 characters and names at most 200.
- **Errors are instructions.** A request that can't be served raises `ToolInputError`,
  and `run_tool()` turns it into an MCP tool error (`isError: true`) whose text the
  model reads:
  - `get_event("Ueno Bank")`: "'Ueno Bank' is not an event id. Event ids look like
    '03b-default-fix:341' (or just the number, 341); get them from get_trending_events.
    To find events about a topic, use ask or search_entities."
  - `find_connection("Bank", ...)`: "No entity is named exactly 'Bank'. Close matches:
    Ueno Bank (organization, id Q137783961); ... Call again with one of these ids or
    names."
  - Neo4j down: "The WikiPulse knowledge graph (Neo4j) is not reachable. Tell the user
    to start it with `docker compose up -d`; retrying now will not help." The last
    clause matters: without it, models tend to retry in a loop.
  - The LLM down (`LLMError` from `ask`): the message names the tools that don't need
    the model.
  Anything else is a real bug: the SDK logs the traceback and the model only sees
  "Error executing tool ...", which is the right amount of detail for it.
- **"Nothing found" is a result, not an error.** An empty trending window, a search
  without matches and two entities without a path return normally with a `note`
  ("No connection within 4 steps. Use get_event on their events, or ask, ..."): the
  model asked a valid question and the answer is "no".
- **Resources and prompts fail as protocol errors.** They have no "error result": an
  unknown `event://...` raises `ResourceNotFoundError` (JSON-RPC code -32602) with the
  same message, and `explain_event` an `MCPError`.

## Design decisions and deviations from the spec

- **SDK version.** `uv add mcp` resolved to `mcp` 2.3.0 (the official SDK's current
  major version). It is the only new dependency; it brings `starlette`, `uvicorn` and
  `sse-starlette` for the HTTP transport.
- **Requirement 1, no duplicated logic.** Every read goes through `QAGraph`, the class
  question answering already uses, so the MCP server inherits its READ-only sessions.
  Four reads were missing and were added there, not in the server
  (`trending_events()`, `event_briefing()`, `entities_by_key()`,
  `entity_event_counts()`), and `QAGraph.events()` now also returns `status`,
  `ended_at` and the best spike `score` (new fields with defaults, so retrieval is
  unchanged). Name matching reuses `fold()`, `STOPWORDS` and `lucene_terms()` from
  `wikipulse.qa`, paths `path_text()`, the hop limit `RetrievalConfig.max_hops`, and
  `ask` is `QASession.ask()` unchanged. The server never writes to the graph.
- **"Active events" became "trending".** In a replayed graph every event has ended (all
  233 events of the snapshot have status `ended`), so "active only" would always be
  empty. `get_trending_events` returns the active events *plus* those that started in
  the `MCP_TRENDING_HOURS` (24) hours before the graph's newest event, the same event
  time "now" that 05a uses for "today". Order: most pages, then best spike score (the
  order of stage 3's feed). The result says `as_of`, so the client model knows which
  "now" it is looking at. An optional `hours` argument changes the window.
- **"Strength" is two numbers**, `page_count` (how many pages spiked together) and
  `spike_score` (the best spike score of its pages: how unusual the editing was), not
  one invented combination.
- **Briefings are read if present.** Spec 05d will store briefings on the `Event` node;
  `event_briefing()` reads the `briefing` property as `properties(ev)['briefing']`, so
  a graph without any briefing yet gives `null` without Neo4j's "property does not
  exist" warning. When 05d defines citations for briefings, `EventDetail` can grow a
  field for them.
- **Event ids are the graph keys** (`run:number`); bare numbers are accepted, with an
  error listing the runs when a number exists in more than one run.
- **Read-only annotations**: `readOnlyHint=True, destructiveHint=False,
  openWorldHint=False` on all five tools (the graph is a closed world; nothing goes out
  to Wikipedia). `idempotentHint` is True except for `ask`, whose wording can change
  between calls. Annotations are hints for clients (e.g. to skip a confirmation
  dialog), not security: what keeps the tools read-only is the READ session.
- **One question at a time.** Tool functions are synchronous; the SDK runs them in a
  worker thread, so the server keeps answering other requests while `ask` waits for
  the model. `LazyAsker` serializes questions with a lock (they share one GPU and one
  session) and creates the `QASession` at the first question, so the server starts fast
  and the other tools never load the models. The first `ask` takes 35 to 45 s.
- **`search_entities` also returns each match's biggest events**, added after the
  routing test (above).
- **No concrete resources are listed.** The SDK's `MCPServer` lists only static
  resources; events are dynamic, so they are offered as the template only. Listing
  current events is what `get_trending_events` is for.
- **Where the code lives.** The spec names `server.py`; the work is in `explore.py` so it
  can be tested without MCP (and reused by 05e's API), and `models.py` holds the output
  shapes.
- **Neo4j query hints are silenced in the server** (logger `neo4j.notifications` at
  ERROR): `allShortestPaths` makes Neo4j send a performance hint on every
  `find_connection`, which filled stderr without telling a tool user anything.
- **Claude Desktop configuration** is an example file in the repo
  (`docs/claude_desktop_config.example.json`) plus the README section; I did not touch
  the human's own Claude configuration.
- **Review fixes.** The example config and the README snippet first wrote the path as
  `"C:\path\to\wikipulse"`, which is invalid JSON (`\p` is not an escape, and `\t` would
  be a tab); in JSON a Windows backslash must be doubled, `"C:\\path\\to\\wikipulse"`.
  `tests/mcp_server/test_client_config.py` now parses both with `json.loads`. The `hours`
  description no longer claims "default 24" (the default is `MCP_TRENDING_HOURS`), and
  the subprocess integration tests kill a server that hangs on shutdown and check that
  each JSON-RPC response's `id` matches its request.
- **The MCP Inspector was not run by me** (see "Results").

## Results

All on 2026-11-09 against the frozen snapshot (`neo4j-eval`, bolt port 7688: 233 events
of 2026-11-05, 1,285 entities; "now" = 2026-11-05 23:22:51 UTC), `QA_MODEL` =
`ollama:qwen3:8b`.

### Acceptance criterion 1: in-process tests for every tool, resource and error case

`tests/mcp_server/test_server.py` connects the SDK's own `Client` to the server object
in-process (no subprocess, no network) and checks, as a client sees them:

- the listing: five tools, the template `event://{event_id}`, the two prompts and their
  arguments; every tool has `readOnlyHint` true, `destructiveHint` and `openWorldHint`
  false, a description and an output schema; the `limit` schema is 1 to 25, default 10;
- each tool's result (structured content, and the same JSON as text);
- the error cases: unknown event, a name instead of an event id, an ambiguous name, the
  same entity twice, an empty question and name, five arguments outside their limits
  (rejected by the schema), Neo4j unreachable (one clear message from four tools), the
  LLM failing in `ask`;
- the resource by key and by number, an unknown event, and Neo4j down;
- both prompts, `daily_briefing` without a graph, `explain_event` with an unknown event;
- `check_host()` with loopback and non-loopback addresses.

`tests/mcp_server/test_explore.py` tests `Explorer` directly (limits, id and name
resolution, ranking, notes). With a fake graph (`ToolGraph`, built on 05a's
`FakeGraph`) and a scripted answerer: 61 unit tests, all passing in about 3 s.

Integration tests (`tests/mcp_server/test_mcp_neo4j.py`, read-only, skipped when the
snapshot server is down): the four new `QAGraph` reads; all tools and the resource on
the real snapshot; **the real server as a subprocess over stdio and over HTTP**; and a
raw JSON-RPC session with protocol version 2025-06-18. One `live` test asks two
questions through `ask` with `qwen3:8b` (55 s): "How are Ueno Bank and Santiago Peña
connected?" is answered with `[1]`-style citations and revision links, and the Federal
Reserve question is refused (`answerable: false`).

### Acceptance criterion 2: "The MCP Inspector lists everything"

The Inspector is a Node.js package (`npx @modelcontextprotocol/inspector`). It was not
installed, and downloading and running it was refused by this environment's permission
policy, so **I could not run the Inspector itself**. What I checked instead is what the
Inspector does when it connects: start the server over stdio, send `initialize` with an
older protocol version, then `tools/list`, `resources/templates/list` and
`prompts/list`. `test_an_older_client_lists_everything_over_raw_json_rpc` does exactly
that in plain JSON-RPC lines and finds the five tools (all `readOnlyHint: true`), the
template and both prompts; a manual run also showed `resources/list` (empty, see
"Deviations"). For the human, to see it in the browser:

```
npx @modelcontextprotocol/inspector uv run python -m wikipulse.mcp_server.server
```

(or with `--neo4j-uri bolt://localhost:7688` for the snapshot). The Tools tab should
list five tools, Resources one template, Prompts two.

### Output sizes

Largest results on the snapshot (pretty-printed JSON): `get_trending_events(limit=25)`
14.3 KB; `get_event` 11.7 KB at most over all 233 events (event 430, 21 entities, 12
relations), median 3.3 KB; no event hit the 25-entity or 20-relation cap;
`search_entities("United States", 25)` 11.5 KB; `find_connection` about 1.3 KB. So no
call puts more than about 4,000 tokens into the client's context.

### Tool choice by a local client model

See "Tool design is prompt design": after two rounds of changes, `qwen3:8b` picked a
sensible first tool for 10 of 10 questions, made no erroneous call (one in the first
run), and no longer offered outside knowledge for a name WikiPulse doesn't know. Times
per question: 2 to 20 s, 36 to 45 s when `ask` runs first (model loading).

### Tests and checks

- `uv run pytest`: 584 passed (61 of them new).
- `uv run pytest -m integration`: 47 passed (5 new), before and after the last change to
  `search_entities`.
- `uv run pytest tests/mcp_server -m live`: 1 passed.
- `uv run ruff check .` and `uv run ruff format --check .`: clean.

## Questions to test your understanding

1. `get_event` is both a tool and the resource `event://{event_id}`, returning the same
   JSON. Why offer both, and who decides when each is used?
2. A model calls `find_connection("Bank", "Peña")`. Walk through what happens in
   `resolve_entity()` for each argument and what the model receives. Why is the result
   an error for one call and not a silent best guess?
3. The tools are annotated `readOnlyHint=True`. If someone added a tool that ran
   `MERGE` through `QAGraph`, would the annotation stop it? What would?

<details>
<summary>Answers</summary>

1. Tools are chosen by the *model* during a conversation (Claude decides it needs
   event 341 and calls `get_event`); resources are chosen by the *user or the app*
   (attaching `event://...341` to a message, as context before the model does
   anything). Offering both lets the event be pulled in either way; the shared
   `Explorer.event()` keeps them identical, and the resource has its own error path
   (`ResourceNotFoundError`, a protocol error) because resources have no error result.
2. "Bank": `lucene_terms()` makes the full-text query `bank`, which finds Ueno Bank,
   Central Bank of Paraguay and others; none is named exactly "bank" (after `fold()`),
   so `ToolInputError` lists the closest matches by `rank()` with their ids. "Peña" is
   never reached, because the first argument already failed. The model gets an
   `isError` result: "No entity is named exactly 'Bank'. Close matches: Ueno Bank
   (organization, id Q137783961); ... Call again with one of these ids or names." A
   silent guess would return a confident path between the wrong entities, which the
   model would present as fact; an error with candidates costs one more call and lets
   the model (or the user) choose.
3. No. Annotations are hints that clients read (for example to skip asking the user
   for confirmation); the server does not enforce them, and the spec warns clients not
   to trust them from untrusted servers. What would stop the write is `QAGraph._session()`,
   which opens every session with `READ_ACCESS`: Neo4j itself rejects writes there
   ("Writing in read access mode not allowed"), as 05a's integration test shows.

</details>

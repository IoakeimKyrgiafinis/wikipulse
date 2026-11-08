# Review: 05c-mcp-server

Reviewed branch `spec-05c-mcp-server` (one commit, `a42247c`) against `main`.

## Acceptance criteria

| Criterion | Result | Evidence |
|-----------|--------|----------|
| In-process tests for every tool, resource and error case | pass | `tests/mcp_server/test_server.py` drives `build_server()` through the SDK's own `mcp.Client`, in process. It covers all five tools, the `event://{event_id}` resource (by key, by number, unknown, graph down), both prompts (including the unknown event and the graph-down cases), bad input for every tool, schema limit violations, the "Neo4j down" path and the "LLM failed" path. `tests/mcp_server/test_explore.py` covers the logic without MCP. `uv run pytest -q`: 584 passed, 50 deselected. |
| The MCP Inspector lists everything | not run literally; the stand-in passes | The Inspector (npx) was not allowed, so it was not run. The stand-in, `test_an_older_client_lists_everything_over_raw_json_rpc`, starts the real server over stdio, sends `initialize` with protocolVersion `2025-06-18`, then `tools/list`, `resources/templates/list` and `prompts/list` as raw JSON-RPC lines. It asserts all 5 tools (each with `readOnlyHint: true`), the template and both prompts, and that the server exits cleanly (code 0). `uv run pytest -m integration tests/mcp_server`: 6 passed. **The human should still run `npx @modelcontextprotocol/inspector uv run python -m wikipulse.mcp_server.server` once before merging** (see the notes below). |
| Req. 1: tools reuse existing modules | pass | `explore.py` reads only through `QAGraph`, which uses `READ_ACCESS` sessions. It also reuses `fold`, `lucene_terms`, `path_text` and `QASession`. Four reads and three `EventInfo` fields were added to `wikipulse/qa/graph.py`; this is an additive change, not duplicated logic. |
| Req. 2: output limits, helpful errors | mostly pass | Clamped limits and `more_*` flags. Every `ToolInputError` names the next tool to call. There is one message for "graph down" and one for "LLM failed". Briefing and relation text are not capped (problem 8). |
| Req. 3: tools annotated read-only | pass | Every tool has `READ_ONLY` or `READ_ONLY_LLM` (`readOnlyHint=True`, `destructiveHint=False`, `openWorldHint=False`). This is asserted in-process and over raw JSON-RPC. No tool writes: every Cypher statement runs in a `READ_ACCESS` session, so Neo4j itself refuses writes, and that includes the LLM-generated Cypher behind `ask`. |
| HTTP transport: loopback only, DNS-rebinding safe | pass | `check_host` refuses `0.0.0.0`, LAN IPs and other hostnames (unit tests). Probed a live `--transport http` server on port 18766. `Host: 127.0.0.1:port` → 200. `Host: evil.example:port` → 421. `Origin: http://evil.example` → 403. `Origin: http://localhost:port` → 200. `Origin: http://localhost:6274` → 403. |
| Live: `ask` with the local model on the snapshot | pass | `pytest -m "integration and live" tests/mcp_server`: 1 passed (qwen3:8b). |
| ruff | pass | `ruff check .`: all checks passed. `ruff format --check .`: 255 files already formatted. |
| Real email not in committed files | pass | grep over every changed file: no match. |

## Problems

1. **medium**, `docs/claude_desktop_config.example.json:7` and `README.md:170`: `"C:\path\to\wikipulse"` is invalid JSON (`json.load` raises "Invalid \escape"; `\t` would also become a tab). Anyone who copies the documented Claude Desktop config and edits only the path gets a config file Claude Desktop can't parse. Use `C:\\path\\to\\wikipulse` or forward slashes.
2. **low**, `wikipulse/qa/graph.py:196-200` (`TRENDING`): any event with `status = 'active'` is included however old it is, and the order is `page_count` first, `score` second. A stale "active" event from an old run can push out recent ones, and the tool description's "strongest first" doesn't say that strength means page count.
3. **low**, `wikipulse/mcp_server/server.py:256`: when the graph is down, the `explain_event` prompt raises `MCPError(INVALID_PARAMS)`, which tells the client its argument was wrong. `INTERNAL_ERROR` fits better.
4. **low**, `wikipulse/mcp_server/server.py:116`: the `hours` description hard-codes "(default 24)", but the real default comes from `MCP_TRENDING_HOURS` and can differ.
5. **low**, `wikipulse/mcp_server/explore.py:45-58`: all output limits (`MAX_LIMIT`, `MAX_HOURS`, `MAX_EVENT_ENTITIES`, ...) are module constants rather than settings in `config.py`. They are defensible as API-contract constants, but they sit at the edge of the AGENTS.md rule on thresholds, and the learning note should say why they stay in code.
6. **low**, `wikipulse/mcp_server/explore.py:368-371`: `search()` runs one `entity_events` query per match, so up to 25 Neo4j round trips per call (N+1).
7. **low**, `wikipulse/mcp_server/explore.py:230`: `get_event` derives `wikidata_id` from the key prefix, while `search_entities` reads the `n.wikidata_id` property. The same field has two sources that could disagree.
8. **low**, `wikipulse/mcp_server/explore.py:246` and `:236-244`: the briefing and the relation subject, relation and object strings are passed through without a length cap. Event descriptions are capped, so requirement 2 is applied unevenly; briefings arrive with spec 05d.
9. **low**, `tests/mcp_server/test_mcp_neo4j.py:148` and `:196`: `process.wait(timeout=30)` in `finally` has no `kill()` fallback. A server that hangs on shutdown raises `TimeoutExpired`, which hides the real failure and leaves an orphaned process holding the port or stdio. The raw JSON-RPC test also doesn't check that each response's `id` matches its request (`:177`).

## Notes for the human (not problems)

- The MCP Inspector stand-in is adequate for what the criterion checks: it covers the protocol handshake with an older protocol version, the four list calls over real stdio, and the annotations. It cannot show whether the Inspector UI renders the input and output schemas, or whether a browser-based "direct" connection works. Run the README's Inspector command once.
- In "direct" mode (no proxy), the Inspector's browser origin (`http://localhost:6274`) gets a 403 from the HTTP transport because of the strict Origin list. This is correct for safety. Use the Inspector's proxy, or stdio.
- Sync tools run in worker threads (the SDK calls `anyio.to_thread.run_sync`), so a slow `ask` does not block other tools on HTTP. `LazyAsker` serialises the questions themselves.
- The routing experiments with qwen3:8b as a stand-in client are described in the learning note. Watching Claude Desktop use the tools (the spec's optional step) is still worth doing.

Verdict: fix

## Questions for the human before merging

1. Every tool is annotated `readOnlyHint=True`, but `ask` makes an LLM write Cypher that runs against your graph. What actually guarantees that this Cypher can't modify the database, and why is the annotation alone not that guarantee?
2. The HTTP transport binds to 127.0.0.1 and also checks the `Host` and `Origin` headers. What attack does the header check stop that binding to loopback alone does not, and why does that matter for a server with no authentication?
3. `get_trending_events` measures "the last 24 hours" from the newest event in the graph, not from `datetime.now()`. What would a user of a replayed recording see if it used wall-clock time, and which events can still show up in the list regardless of the window?

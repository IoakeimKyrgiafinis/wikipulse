# Learning note: 05e API and live web page

## What was built

WikiPulse now has a front door for people: a web page where events appear while they
are being detected, each with its Wikipedia pages, the entities and relations of the
knowledge graph, the analyst's briefing, and a box to ask the graph a question. Behind
it is a small FastAPI backend that other programs can use too.

```
uv run python -m wikipulse.api                                # http://127.0.0.1:8000, run "live"
uv run python -m wikipulse.api --run-id 03b-default-fix       # the recorded day
uv run python scripts/demo_live_events.py --reset             # a recorded day, re-grouped slowly
uv run python -m wikipulse.api --run-id 05e-live-demo         # ... and watched live
```

| File | What it does |
|------|--------------|
| `wikipulse/api/app.py` | `create_app()`: the routes `GET /events`, `GET /events/{event_id}`, `POST /ask`, `GET /health`, the WebSocket `/ws/events` and the page at `/`; `BodyLimit` (request size), `QuestionGate` (questions waiting), host and origin checks, security headers. `build_app()` wires the real parts from the settings. |
| `wikipulse/api/events.py` | `EventReader`: joins grouping's view of an event (Postgres) with the graph's (Neo4j, through `QAGraph` and the MCP server's `Explorer`). `PostgresEvents` is the Postgres side; `group_pages()`, `parse_event_id()`, `briefing_out()`. |
| `wikipulse/api/live.py` | `EventWatcher` polls the events and diffs them with the last poll; `Broadcaster` fans each change out to one bounded queue per WebSocket client. |
| `wikipulse/api/models.py` | The pydantic models of every request and response (`EventCard`, `EventDetailOut`, `AskRequest`, `Health`, ...), which FastAPI turns into the docs at `/docs`. `AskResult` and the entity and relation models are the MCP server's, reused. |
| `wikipulse/api/__main__.py` | `uv run python -m wikipulse.api`: `--run-id`, `--host`, `--port`, `--neo4j-uri`; runs uvicorn on a loopback address only. |
| `wikipulse/api/static/index.html`, `app.js`, `style.css` | The page: plain HTML, CSS and JavaScript, no build step, nothing loaded from another site. |
| `wikipulse/db/events.py` | `recent_events()` (one run's events, most recent activity first, with their pages) and `latest_event_time()`; `FeedEvent` gained `merged_into`. |
| `wikipulse/qa/graph.py` | One new read, `QAGraph.event_overviews(keys)`: headline, description, entity count and briefing metadata of many events in one query. |
| `wikipulse/config.py`, `.env.example` | `API_HOST`, `API_PORT`, `API_RUN_ID`, `API_POLL_INTERVAL_S`, `API_WATCH_LIMIT`, `API_MAX_WAITING_QUESTIONS`. |
| `scripts/demo_live_events.py` | Re-groups a recorded day step by step so the page has a "live run" to show without the live pipeline. |
| `tests/api/` | 69 unit tests (fakes for Postgres, the graph and the LLM; FastAPI's test client, including the WebSocket) and 3 integration tests (real Postgres and the real grouper; Neo4j read-only). |

How the pieces fit:

```
pipeline (unchanged)                         API process (wikipulse.api)
─────────────────────                        ─────────────────────────────────────────────
grouper (03b) ──► Postgres events,  ◄──┐     EventWatcher.run(): every API_POLL_INTERVAL_S
                  event_pages          ├──── EventReader.cards(200 most recent)
graph runner (04b) ─► Neo4j Event,     │        Postgres: recent_events()   (seconds after detection)
agent (05d) ───────►  entities,     ◄──┘        Neo4j:    event_overviews() (after extraction / briefing)
                      briefing               diff with the last poll ─► "new" / "updated"
                                                    │
                                             Broadcaster.publish() ─► one queue per client
                                                    │
browser ◄──── WebSocket /ws/events ◄────────────────┘   snapshot on connect, then changes
   │
   ├── GET /events/{id} ─► EventReader.detail() ─► Postgres pages + Explorer.event() (05c)
   └── POST /ask ────────► QuestionGate ─► Explorer.ask() ─► QASession.ask() (05a)
```

## Key concepts

### Backend API design with FastAPI

FastAPI builds an HTTP API from ordinary Python functions with type hints. In
`_add_routes()` in `app.py`:

```python
@app.get("/events", tags=["events"])
def list_events(
    limit: Annotated[int, Query(ge=1, le=MAX_LIST, description="Most events.")] = 50,
    status: Annotated[Literal["all", "active", "ended"], Query(...)] = "all",
    min_pages: Annotated[int, Query(ge=1, le=100, ...)] = 1,
) -> EventList:
```

Three things come out of these few lines:

- **Validation.** `GET /events?limit=500` never reaches the function: FastAPI checks the
  query against `ge`/`le` and the `Literal` and answers **422 Unprocessable Entity** with
  what was wrong. The same for request bodies: `AskRequest` (in `models.py`) declares
  `question: str = Field(min_length=1, max_length=500)` and `extra="forbid"`, so an
  empty question, a 501-character one, a number, or an unknown field is refused before
  any model runs.
- **Serialization.** The return type `EventList` is a pydantic model; FastAPI turns it
  into JSON (datetimes as ISO 8601) and *filters* the output to the declared fields.
- **Documentation.** Every route, parameter, limit and model (with the `description`s
  of its fields) appears in the OpenAPI schema at `/openapi.json` and the interactive
  docs at `/docs`, generated from the same code, so they can't drift apart.
  `test_the_api_documents_itself` checks that the 500-character limit is in the schema.

Design choices worth noticing:

- **Status codes say who must act.** 404 (no such event), 422 (your request is wrong,
  with a message saying how), 413/411 (body too big or of unknown size), 429 (too many
  questions waiting: try later), 503 (a service behind the API is down: Postgres,
  Neo4j or the LLM, with what to start). `postgres_down()` maps any
  `psycopg.OperationalError` to 503 in one place.
- **Sync and async routes.** `list_events` and `get_event` are plain `def`: they block on
  Postgres and Neo4j, so FastAPI runs them in a worker thread and the event loop stays
  free for WebSockets. `/ask` is `async` and hands the slow call to a thread explicitly
  (`anyio.to_thread.run_sync(explorer.ask, ...)`), because it first has to check the
  `QuestionGate` on the event loop.
- **Limits on input (requirement 2).** Length and type by the model; whitespace is
  collapsed and an all-blank question refused by `Explorer.ask()` (05c's check, reused);
  `BodyLimit` refuses any body over 8 KB *before* it is read (a 500-character question
  is at most about 2 KB as JSON), so nobody can make the API parse megabytes; and
  `QuestionGate` turns questions away with 429 once `API_MAX_WAITING_QUESTIONS` (3) are
  running or waiting, since they run one at a time on one GPU (05c's `LazyAsker` lock).
  The 422 responses leave out FastAPI's usual `input` field (`invalid_request()`), so a
  long or hostile input is not echoed back.
- **Dependency injection by construction.** `create_app(reader, explorer, ...)` takes
  its parts ready-made, so the tests pass fakes (`tests/api/helpers.py`) and
  `build_app(settings)` passes the real ones. No globals, no monkeypatching.

### Real-time web: WebSockets pushing events

HTTP is request and response: the browser asks, the server answers, done. To show a new
event the moment it exists, the server must speak first. A **WebSocket** is a
connection that starts as an HTTP request (`GET /ws/events` with `Upgrade: websocket`)
and then stays open in both directions, so the server can send a message whenever it
has one. (The alternatives: the page polls `GET /events` every few seconds, which is
simple but always late and mostly wasted requests; or Server-Sent Events, one-way
server pushes over HTTP, which would also have worked here.)

The protocol on top is ours, three JSON message types (`live.py`'s docstring):

```json
{"type": "snapshot", "ready": true, "as_of": "...", "events": [ ...cards... ]}
{"type": "event", "change": "new", "as_of": "...", "event": { ...card... }}
{"type": "pong"}
```

On connect, the client gets a **snapshot** (the current list), then only **changes**.
`EventWatcher.subscribe()` adds the client and takes the snapshot in one synchronous
step: everything runs on one event loop, so nothing can be published in between, and a
client never misses a change or receives one older than its snapshot.

Where the changes come from: the pipeline doesn't know the API exists. The watcher
**polls**: every `API_POLL_INTERVAL_S` (5) seconds `EventWatcher.poll_once()` reads the
200 most recent events (`EventReader.cards()`: Postgres plus the graph), compares each
card with the previous poll's (`diff()`: pydantic models compare field by field), and
publishes "new" for unseen ids and "updated" for changed ones. Because the card
includes the graph's fields, the same diff catches every stage: a page joining (03b), the
headline and entities arriving (04b), a briefing written (05d), an event ending or
merging. Polling every 5 s costs two small queries; a push from the database
(Postgres `LISTEN/NOTIFY`, a Redis channel) would be faster but would touch the
grouper, the graph runner and the agent. Stage 6b is about event-driven designs.

**Cleaning up clients (requirement 3).** A WebSocket can end in many ways: the tab is
closed (a close frame arrives), the laptop sleeps (nothing arrives at all), the client
is too slow. `events_socket()` runs two tasks per connection in an anyio task group:

- `_send_loop()` takes messages from the client's queue and sends them;
- `_receive_loop()` reads from the socket. Its only real job is to *notice the
  disconnect*: without a pending read, a server that has nothing to send would never
  find out the client is gone.

Whichever ends first cancels the other, and the `finally` in `events_socket()` always
calls `broadcaster.unsubscribe(client)`. uvicorn also pings every client every 20
seconds at the protocol level, so a silently vanished one fails its read within about
40 s. Each client's queue is bounded (`CLIENT_QUEUE_SIZE` = 500): a client that falls
that far behind is dropped (`Broadcaster._drop()`) and its connection closed with code
1013 ("try again later") rather than letting memory grow; the page reconnects and gets a
fresh snapshot. `test_disconnected_clients_are_cleaned_up` and
`test_a_client_that_falls_behind_is_disconnected` check both paths, and `/health`
reports `websocket_clients`.

On the page (`app.js`), `connect()` opens the socket, `onMessage()` replaces the list on a
snapshot and upserts a card on each change (flashing it yellow; a "new" badge for events
that arrived live), and on `close` it reconnects with a doubling delay (1 s up to 30 s).

### Connecting a pipeline to users

This is where the data system meets a product, and the product view changed some
decisions:

- **Show it early, improve it in place.** An event exists in Postgres seconds after the
  grouper sees its first spike, but its headline and entities come minutes later from
  the LLM (04b), and a briefing later still (05d). The page shows the event right away,
  titled after its strongest page ("Storm Amy"), marked "not in the graph yet", and the
  card updates itself when the graph catches up ("Storm Amy hits Ireland", 4
  entities, briefing). That is why the list comes from Postgres and the details from the
  graph, joined by the shared key `run:number` (`EventReader.cards()`), instead of
  listing only what the graph has (which would show events minutes late).
- **One source of logic.** Event detail is `Explorer.event()` and questions are
  `Explorer.ask()`, the code behind the MCP tools `get_event` and `ask` (05c), which in
  turn reads through `QAGraph` and answers with `QASession` (05a). The API adds no
  retrieval or prompting of its own, so the page, Claude Desktop and the analyst agent
  see the same facts. Briefings are read from the properties 05d stored on the `Event`
  node; `briefing_out()` splits the stored text from its "Sources:" block and pairs the
  `[n]` markers with `briefing_sources`/`briefing_urls`, so the page can link each
  marker.
- **Degrade, don't fail.** If Neo4j goes away, the list keeps the last graph data it
  read (`EventReader.overviews()` keeps a cache that is only used during an outage), the
  detail shows the Postgres side with a note, `/ask` answers 503, and `/health` says
  "degraded". If Postgres goes away, the routes answer 503 and the watcher keeps its
  last list and logs once until it works again.
- **Event time on the screen.** "data up to Nov 5, 23:58 UTC" is the newest event time
  in the data (`as_of`), not the clock: for a replayed day it says when the data ends,
  which is what a reader needs to interpret "active".
- **Untrusted text stays text.** Titles, summaries and model output come from Wikipedia
  editors and an LLM. `app.js` builds every element with `textContent` (never
  `innerHTML`), makes links only of `https://en.wikipedia.org/` URLs, and the page is
  served with a strict Content-Security-Policy (`default-src 'none'; script-src 'self';
  connect-src 'self' ...`), so even an injected script tag would not run.

## Design decisions and deviations from the spec

- **The list comes from Postgres, the details from the graph.** The spec says "events
  appear live as they're detected". Detection and grouping write Postgres; the graph
  only has an event after the LLM extraction of 04b. Listing from the graph alone
  (`Explorer.trending()`) would reuse more code but show every event minutes late, so
  `EventReader` joins both on the key `run:number`. The detail of an extracted event is
  `Explorer.event()` unchanged; the API adds the Postgres pages (with spike counts and
  times) and a note when the graph has nothing yet.
- **One grouping run per API.** `API_RUN_ID` (default `live`, `--run-id` on the command
  line) names the run the list and the WebSocket show, like the other runners'
  `--run-id`. `GET /events/{id}` also accepts `other-run:12`, read-only.
- **Ids are `run:number`**, the graph's key and the MCP tools' `event_id`; a bare number
  means the API's run. Anything else is a 422 with an example of a valid id.
- **Order of the list**: active events first, then by their latest spike start
  (`RECENT_EVENTS` in `db/events.py`; the page sorts the same way in `order()`), so a
  story that just gained a page moves up. Stage 3's feed orders by size, which suits a
  report but not a live view.
- **Polling, not notifications.** See "Real-time web". Every 5 s, the 200 most recent
  events (`API_WATCH_LIMIT`); a change outside that window is not pushed, which a live
  page doesn't need.
- **Merged events** are pushed once with `status: "merged"` and `merged_into`, so a page
  can drop them; snapshots and `GET /events` leave them out.
- **The page is three files**, `index.html`, `app.js` and `style.css`, not one. The
  Content-Security-Policy `script-src 'self'` forbids inline scripts, which is what makes
  it a real defense against injected markup. Still plain files, no build step.
- **Security without accounts.** There is no login (none was asked for), so the API is
  for this machine only: `check_host()` from 05c refuses non-loopback addresses,
  `TrustedHostMiddleware` refuses requests whose `Host` isn't a loopback name (DNS
  rebinding), and `origin_allowed()` refuses WebSocket connections from pages of other
  sites. Kubernetes (stage 8) will need to listen on all interfaces inside a container;
  that is a change for that spec.
- **`/health` always answers 200** while the process runs, with `status: "degraded"` and
  which service is down. A failing health check usually means "restart me", and
  restarting the API doesn't bring Postgres back.
- **`/docs` needs the internet**, the page doesn't. FastAPI's Swagger UI loads its
  JavaScript and CSS from a CDN; requirement 1 is about the page, which loads nothing
  from outside (`test_the_page_loads_nothing_from_outside`). Offline, the schema is still
  at `/openapi.json`. Links to Wikipedia on the page need the internet only when clicked.
- **Dependencies.** `fastapi` (allowed from this stage), `uvicorn` (already installed by
  the MCP SDK, now listed because the API imports it) and `websockets` (uvicorn has no
  WebSocket support of its own; without a WebSocket library it cannot accept upgrades).
- **`scripts/demo_live_events.py` is an addition.** No live pipeline was running (and a
  fresh detector needs hours of baseline before it reports spikes), so the acceptance
  check used a recorded day's spikes, re-grouped by the real `GroupingRunner` 15 minutes
  of event time every 10 seconds. The human can use it to look at the page without
  starting the stream.
- **The demo was not extracted into the graph.** That would have written LLM output to
  Neo4j, and this spec only reads the graph. The updates from extraction and briefings
  (a card gaining a headline, entities, a briefing) are covered by the unit tests
  (`test_changes_to_an_event_are_pushed_as_updates`) and by looking at the recorded day,
  which is extracted and has five briefings.
- **Small page fixes after looking at it.** The "new" badge first stayed on every event
  that arrived live, so after a few minutes of the demo all 148 cards said "new"; it now
  disappears after two minutes (`NEW_BADGE_MS`). A citation whose source has no link
  (the event's own title and description, cited by 05d briefings) is now styled like
  the others. Answer sources show their revision, because one answer can cite two
  revisions of the same page.

## Results

All on 2026-11-09, on this machine, with Postgres, Neo4j (main graph on 7687, snapshot
on 7688) and Ollama (`qwen3:8b`) running.

### Acceptance criterion 1: API tests, and a WebSocket test receiving a pushed event

`tests/api/` uses FastAPI's `TestClient`, which runs the app, its lifespan and its
watcher in a background thread without a network:

- `test_app.py` (36 tests): listing (order, graph headline vs. page title, distinct
  pages vs. spikes, filters, bad parameters), detail (ids in three forms, briefing split
  into text and sources, revision links, an event not yet extracted, Neo4j down), `/ask`
  (a cited answer, six invalid bodies, the 500-character limit, blank questions, bodies
  over 8 KB and without a length, the LLM or Neo4j failing, 429 when too many wait),
  `/health`, the page and its security headers, "nothing loaded from outside", the
  OpenAPI schema, and a foreign `Host` refused.
- `test_websocket.py` (8 tests): the snapshot on connect; **`test_a_new_event_is_pushed`**
  (an event added to the rows arrives as `{"type": "event", "change": "new", ...}`);
  updates when a page joins and when extraction adds a headline; nothing pushed when
  nothing changed; merged events; **disconnected clients removed** (requirement 3); a
  client that falls behind closed with 1013; another site's origin refused.
- `test_live.py` and `test_events.py` (25 tests): the diff, the watcher's first
  snapshot, a failing poll, dropping a slow client, page grouping, id parsing, briefing
  splitting, the outage cache.
- `test_api_db.py` (3 integration tests): the Postgres queries on a test run built by
  the real grouper; **the live path end to end** (the real `GroupingRunner` stores an
  event in Postgres, the watcher sees it, a WebSocket client receives it, the detail
  shows its page); `QAGraph.event_overviews()` on the frozen snapshot.

No WebSocket test can hang: before reading a pushed message, a test waits (with a
deadline) until the watcher has seen the change, then sends "ping" and reads up to the
"pong", which always comes.

### Acceptance criterion 2: during a live run, events appear without refreshing

Checked with a real browser, headless Chrome driven over the DevTools protocol (a
throwaway script, not committed; the timeline is in `data/api/browser-timeline.jsonl`):

1. Started `uv run python -m wikipulse.api --run-id 05e-live-demo --port 8012` (an empty
   run) and opened the page in headless Chrome. The page connected (`live`, one
   WebSocket client in `/health`), with an empty list.
2. Set a marker variable in the page (`window.__probe`) that a reload would erase, then
   started `scripts/demo_live_events.py`: spikes of run `02b-loose` from 16:00 to 20:00
   UTC on 2026-11-05, 15 minutes of event time every 10 seconds.
3. Read the page's state every 5 seconds for 230 seconds.

| Seconds | Events on the page | Data up to (event time) |
|---:|---:|---|
| 0 to 35 | 0 (the grouper loading its model) | |
| 40 | 6 | Nov 5, 16:14 UTC |
| 60 | 25 | 16:44 |
| 90 | 51 | 17:29 |
| 125 | 74 | 18:14 |
| 175 | 117 (Ueno Bank on top) | 19:29 |
| 195 to 230 | 148 | 19:59 |

The marker survived all 46 readings: **the page was never reloaded**; every event
arrived over the WebSocket. The API pushed 273 messages in 16 polls (148 new events and
125 updates: pages joining, spikes and events ending), one batch per replay step, at
most one poll interval (5 s) after the grouper wrote it. Clicking the newest card loaded
its detail ("Not in the knowledge graph yet ..." with its Wikipedia page). The grouper's
own summary: 172 spikes, 148 new events, 24 joins. The demo run was deleted afterwards
(`--delete`).

Then, on the recorded day (`--run-id 03b-default-fix`, extracted and briefed), a
screenshot of event 341 showed the headline, the briefing with linked `[n]` markers and
its three sources, five pages with revision links, 16 entities and 9 relations; and a
question typed into the box, "Who is involved in the Paraguayan banking crisis?", was
answered in 31 s with six numbered sources. Over `curl`, "How are Ueno Bank and
Santiago Peña connected?" took 98 s as the first question (loading the embedding model
and the LLM). Screenshots: `data/api/page-event.png`, `page-answer.png`,
`page-briefing.png` (not committed).

### What was confusing (the spec's optional "look at it yourself", in part)

There was no real live run to watch for 15 minutes; from the demo and the recorded day:

- **Most events are one page.** 543 of the recorded day's 572 events have a single page,
  many not news (*Taurotragus*, *Sonority sequencing principle*). The page therefore
  starts with the "Pages 2+" filter; "1+" shows everything.
- **A replayed day shows only its last 200 events.** The watcher (and the snapshot)
  holds the 200 most recently active events, so on the recorded day "3+ pages" shows 5
  events, not the 18 significant ones of the whole day. Live, 200 events cover several
  hours. `API_WATCH_LIMIT` raises it.
- **"active" on a replayed day** means "still spiking when the data ends", not "now";
  the header's "data up to ..." is there to make that readable.
- **Before extraction, titles are page titles** ("Ueno Bank" rather than "Paraguayan
  president resigns ..."): the card says "not in the graph yet", and the title changes in
  place once stage 4 has run.

### Tests and checks

- `uv run pytest`: 727 passed (69 of them new).
- `uv run pytest -m integration`: 53 passed (3 of them new).
- `uv run ruff check .` and `uv run ruff format --check .`: clean.

## Questions to test your understanding

1. A browser connects to `/ws/events` at the same moment the watcher finds a new event.
   Why can't the client end up with a snapshot that lacks the event *and* miss the
   "new" message for it?
2. A user closes the laptop lid with the page open. Nothing is ever sent by that
   browser again, and no close frame arrives. How does the API notice, and what would
   happen without `_receive_loop()`?
3. `/ask` limits a question to 500 characters with pydantic. Why is `BodyLimit` still
   needed, and why does it refuse a body without a `Content-Length` instead of counting
   bytes as they arrive?

<details>
<summary>Answers</summary>

1. Everything that touches the subscribers runs on one event loop, and the two critical
   steps contain no `await`: `EventWatcher.subscribe()` adds the client and takes
   `self.snapshot()` in one synchronous call, and `poll_once()` replaces `self.cards`
   and publishes the diff with no `await` in between (its only `await` is the database
   read before). So either the poll's update ran first (the snapshot already contains
   the event, and the client wasn't subscribed yet to get it twice) or the client
   subscribed first (the snapshot lacks it, and the "new" message lands in its queue).
   The page upserts by `event_id`, so even a duplicate would be harmless.
2. uvicorn sends a WebSocket ping every 20 seconds and closes the connection when no
   pong comes back within 20 more seconds. `_receive_loop()` is waiting in
   `receive_text()`, which then raises `WebSocketDisconnect`; its `finally` cancels the
   task group, and `events_socket()`'s `finally` calls `broadcaster.unsubscribe()`.
   Without a pending read nobody would collect that disconnect: the send loop only
   notices a dead socket when it tries to send, so on a quiet run the client would stay
   in the broadcaster (and `/health` would count it) until the next event.
3. Pydantic validates *after* the whole body has been received and parsed as JSON, so a
   100 MB body would still be read into memory and parsed before the 500-character rule
   rejected it. `BodyLimit` looks at the declared length first and refuses before
   reading anything. A chunked body declares no length; counting while reading would
   mean failing in the middle of a request FastAPI is already parsing, which is harder to
   do cleanly, and every real client (a browser's `fetch`, curl, httpx) sends a length
   for a JSON body anyway. So "no length" gets 411 Length Required.

</details>

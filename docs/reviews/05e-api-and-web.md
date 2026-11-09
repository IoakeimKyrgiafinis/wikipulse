# Review: 05e-api-and-web

Reviewer: independent agent. Branch `spec-05e-api-and-web` (1 commit, fa35206), diffed against `main`.

## What I ran

- `uv run pytest`: 727 passed, 57 deselected.
- `uv run pytest tests/api -m "integration or not integration"`: 72 passed (includes the 3 integration tests on real Postgres and Neo4j, plus the snapshot on 7688).
- `uv run ruff check .`: clean. `uv run ruff format --check .`: 301 files already formatted.
- Started `uv run python -m wikipulse.api --run-id 03b-default-fix --port 8017` on 127.0.0.1 and probed it with curl and a Python `websockets` client. Stopped it afterwards.
- Searched the added lines of the diff for e-mail addresses (none) and for committed `data/` files (none).

## Acceptance criteria

| Criterion | Result | Evidence |
|---|---|---|
| API tests with FastAPI's test client | pass | `tests/api/test_app.py` (36 tests), `test_events.py` and `test_live.py` use `TestClient` and fakes, with no network. They cover listing, filters, bad parameters, detail by three id forms, briefing splitting, Postgres or Neo4j down, `/ask` validation (empty, 501 chars, wrong type, extra field, non-object, whitespace only), 413/411 body limits, 429, `/health`, CSP headers, the OpenAPI schema, and a foreign Host refused. All pass. |
| A WebSocket test receiving a pushed event | pass | `tests/api/test_websocket.py::test_a_new_event_is_pushed` adds a row, waits until the watcher sees it, then reads `{"type":"event","change":"new",...}`. The integration test `test_an_event_the_grouper_stores_is_pushed_to_a_websocket_client` runs the real `GroupingRunner`, then Postgres, the watcher and a WebSocket client. Both pass. |
| During a live run, events appear on the page without refreshing | pass (with caveat) | No live stream was running. The implementer replayed a recorded day through the real grouper (`scripts/demo_live_events.py`) and drove headless Chrome. `data/api/browser-timeline.jsonl` has 47 readings: the count goes from 0 to 148 events and the `marker` set once in the page survives every reading, so the page never reloaded. I did not drive a browser myself. I did verify the server side live: snapshot on connect (200 events), ping/pong, and the end-to-end integration test. I also read `app.js`: it upserts by `event_id` and re-renders without reloading. |
| Req 1: the page works with no internet | pass | `index.html`, `app.js` and `style.css` reference only `/static/...`, and the WebSocket and fetch calls go to `location.host`. CSP `default-src 'none'; script-src 'self'; ... connect-src 'self'` is confirmed in the live response headers. `/docs` needs a CDN; this is documented as a deviation and is not the page. |
| Req 2: input validation and length limits on `/ask` | pass | Live: a 9 KB body gives 413. A `text/plain` body gives 422 (FastAPI strict content-type, so cross-site form posts can't trigger questions). Bad JSON gives 422 without echoing the input. `max_length=500` and `extra="forbid"` are in the model, and whitespace-only questions give 422. |
| Req 3: disconnected WebSocket clients are cleaned up | pass | `test_disconnected_clients_are_cleaned_up` and `test_a_client_that_falls_behind_is_disconnected` pass. Live: after 7 connect/disconnect cycles, `/health` reported `websocket_clients: 0`. |

### Web safety probes (live server)

- Host check: `Host: evil.example` gives 400. `localhost:8017` and `[::1]:8017` give 200. `127.0.0.2:8017` gives 400 (see problem 2).
- WebSocket Origin: no Origin is accepted. `http://127.0.0.1:8017` is accepted. `https://evil.example`, `null` and `http://127.0.0.1.evil.example` are refused with 403. `http://localhost:9999` is accepted (see problem 1).
- WebSocket frame of 200 KB: closed with 1009 (`ws_max_size` 64 KB).
- CORS preflight to `/ask` from another origin: 405, with no `Access-Control-*` headers.
- `/events/%3Cscript%3E` gives 422 with a plain JSON message. `/static/..%2F..%2Fconfig.py` gives 404.
- XSS: every data value in `app.js` goes through `textContent`, `append(String)` or `setAttribute` on non-URL attributes. `href` is set only for URLs starting with `https://en.wikipedia.org/`. There is no `innerHTML` anywhere.
- Gate: 5 concurrent `/ask` requests gave two 429s at once. `/health` answered in 50 ms while questions ran, so the event loop is not blocked. One question, once the gate freed, gave 200 in 13 s with 4 cited revisions.

## Problems

1. **low**, `wikipulse/api/app.py:247-253`: `origin_allowed()` compares only the Origin's hostname, not its scheme or port, so any page served from another local port (verified: `http://localhost:9999`) can open `/ws/events`. The data is public and read-only, and non-browser local processes can connect anyway, so the impact is small.
2. **low**, `wikipulse/api/__main__.py:354` / `wikipulse/api/app.py:46`: `check_host()` accepts any 127.0.0.0/8 address, but `LOOPBACK_HOSTS` lists only `127.0.0.1`, so `--host 127.0.0.2` starts a server that answers 400 to every request (verified with `Host: 127.0.0.2:8017`).
3. **low**, `wikipulse/api/app.py:89-104` and `.env.example` (API_MAX_WAITING_QUESTIONS): the gate counts the running question as well as the waiting ones, so the default 3 means 1 running plus 2 waiting, while `.env.example` says "with more than this many waiting".
4. **low**, `wikipulse/api/app.py:229`: the `LLMError` text is passed through to the HTTP client in the 503 detail, which can expose backend details such as model names or Ollama URLs. Loopback only, so minor.
5. **low**, `wikipulse/api/events.py:42`: `WIKIPEDIA_PAGE = "https://en.wikipedia.org/wiki/"` is hard-coded while `config.py` already defines the wiki base URLs (`wiki_api_url`). This follows the existing precedent in `qa/types.py`, but the wiki host is a setting.
6. **low**, `wikipulse/api/events.py:205-219`: `EventReader._overviews` is read and mutated both from the watcher's worker thread and from FastAPI's threadpool (sync routes) without a lock. A `clear()` racing the outage branch's `key in` / `[key]` lookup could raise `KeyError` (rare).
7. **low**, `wikipulse/api/static/app.js:231-233`: the error branch of `loadDetail()` does not check `state.selected === eventId`, so a late failure for a previously clicked event overwrites the detail of the event now selected.
8. **low**, `wikipulse/api/static/app.js:256`: "Wikipedia pages (N)" shows the server-capped count (`MAX_EVENT_PAGES` = 30) with no "+" marker, unlike entities and relations.
9. **low**, `wikipulse/api/live.py:147-157` with `tests/api/test_websocket.py:56-61` and `tests/api/test_api_db.py:114-116`: the tests wait on `watcher.cards`, which is assigned before `publish()` runs. A thread switch in between could let the test send "ping" before the event is queued, so a theoretical flake is possible.
10. **low**, `wikipulse/api/app.py:156-161`: `nosniff` and the other security headers are set only on `/` and `/static/*`, not on the JSON API responses.

Counts: high 0, medium 0, low 10.

No changes are outside the spec's scope. The spec has no "Out of scope" section. The additions to `db/events.py` (`recent_events`, `latest_event_time`, `merged_into`) and `qa/graph.py` (`EventOverview`, `event_overviews`) are read-only and only serve the API. `scripts/demo_live_events.py` is an extra file, documented in the learning note. No tests were weakened or skipped. The new dependencies (`fastapi`, `uvicorn`, `websockets`) are justified in `pyproject.toml` and the learning note. Settings are in `config.py` and `.env.example`. There is no `print` in the package and no `datetime.now()` in the processing logic (`as_of` is event time). No e-mail address appears in the diff.

Verdict: merge

## Questions for the human before merging

1. The page learns about new events by polling Postgres every `API_POLL_INTERVAL_S`, not by being notified by the grouper. What is the worst-case delay between the grouper storing an event and the browser showing it, and which changes are never pushed at all (think about `API_WATCH_LIMIT`)?
2. Three separate checks guard the API: `check_host()`, `TrustedHostMiddleware` and `origin_allowed()`. Which attack does each one stop, and why is a WebSocket connection with no `Origin` header still accepted?
3. A user submits a question and then closes the tab after 2 seconds. What happens to the model call, and when does the `QuestionGate` slot become free again? What would a fourth user see in the meantime?

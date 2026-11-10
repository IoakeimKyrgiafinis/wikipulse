# WikiPulse

WikiPulse watches Wikipedia's live edit stream, detects topics that suddenly attract
unusual editing (usually breaking news), groups them into events, builds a knowledge graph
of the people, organizations and places involved, and answers questions about them.

This is an early skeleton; see `docs/ROADMAP.md` for the plan.

## Setup

1. Install [Git](https://git-scm.com/), [uv](https://docs.astral.sh/uv/) and
   [Docker Desktop](https://www.docker.com/products/docker-desktop/).
2. Copy the example settings, then put your own contact address in `USER_AGENT`
   (Wikimedia requires it):
   - PowerShell: `Copy-Item .env.example .env`
   - bash: `cp .env.example .env`
3. Install Python and the dependencies: `uv sync`
4. Start Redis, Postgres, Neo4j and the object store: `docker compose up -d` (check with `docker compose ps`)
5. Check that the services are reachable: `uv run python -m wikipulse.check`
6. Run the tests: `uv run pytest`

Lint and format with `uv run ruff check .` and `uv run ruff format .`.

## Stage 1: live stream consumer

With the services running and `USER_AGENT` set in `.env`:

| Command | What it does |
|---------|--------------|
| `uv run python -m wikipulse.stream.consume` | Live consumer: English Wikipedia article edits into the Redis Stream `wiki:edits`. Ctrl+C stops it cleanly; it resumes from its checkpoint on the next start. |
| `uv run python -m wikipulse.stream.stats --watch` | Counters, events per second, bot share, stream length and lag, refreshed every 2 seconds. |
| `uv run python -m wikipulse.stream.record --minutes 60` | Saves the raw, unfiltered stream to `data/recordings/<start time>.jsonl`. |
| `uv run python -m wikipulse.stream.replay --file <path> --speed max` | Feeds a recording through the same processing core (`--speed 10` for 10x real time) into `REPLAY_STREAM_KEY` (`wiki:replay:edits`), not the live stream. |
| `uv run pytest -m integration` | Integration tests against the Docker Redis and Postgres. |
| `uv run python scripts/crash_test.py` | Five-minute crash test: kills and restarts the consumer, then checks nothing was lost or doubled. |

## Stage 2: spike detection

| Command | What it does |
|---------|--------------|
| `uv run python -m wikipulse.db.migrate` | Applies new SQL migrations (`wikipulse/db/migrations/`); run once before detecting. |
| `uv run python -m wikipulse.detect.run` | Live detector: reads `wiki:edits` with a consumer group, stores spikes in Postgres as run `live`. Ctrl+C saves its state; it resumes on the next start. |
| `uv run python -m wikipulse.detect.trending` | Prints the active spikes (`--all` adds ended ones, `--watch 30` refreshes, `--run-id` picks a run). |
| `uv run python -m wikipulse.detect.run --stream-key wiki:replay:edits --run-id <name> --reset --exit-when-idle 10` | Detects spikes in a replay, from scratch, as its own run. |
| `uv run python scripts/determinism_check.py --file <recording>` | Replays a recording twice (one run restarted half way) and checks the spikes are identical. |
| `uv run python scripts/compare_thresholds.py --file <recording> --set min_edits=4` | Tries threshold changes on a recording in seconds, offline. |

All thresholds are `DETECT_*` settings (see `.env.example`).

### Evaluating detection

| Command | What it does |
|---------|--------------|
| `uv run python scripts/detect_recordings.py --file <rec> [--file <rec>] --variant <run>:min_edits=4` | Detects spikes in days of recordings in about a minute (offline, same results as a replay) and stores them as run `<run>`. |
| `uv run python -m wikipulse.eval.label --run-id <run> --from <date> --to <date>` | Labels a random sample of 50 spikes as news / not news / unsure with a local LLM (`EVAL_LABEL_MODEL`); `--manual` lets you label them yourself. Saved to `evals/labels/spike_labels.jsonl`. |
| `uv run python -m wikipulse.eval.detection --from <date> --to <date> --name <run>` | Measures recall, precision and time to detection against Wikipedia's "Current events" portal; writes `evals/results/<today>_<run>.md` and `.json`. Pass the run's threshold changes with `--set`. |
| `uv run python -m wikipulse.eval.detection --compare <run A> <run B>` | Prints how the metrics of two reports differ. |

## Stage 3: event grouping

### Page enrichment

Fetches each spiking page's canonical title, summary, Wikidata ID, categories and
outgoing article links into the Postgres table `pages` (run `uv run python -m
wikipulse.db.migrate` first). API responses are cached in the table `api_cache` for
`ENRICH_CACHE_TTL_H` hours, and requests are limited by `WIKI_API_CONCURRENCY` and
`ENRICH_MIN_INTERVAL_S`.

| Command | What it does |
|---------|--------------|
| `uv run python -m wikipulse.enrich.run` | Live: every `ENRICH_POLL_INTERVAL_S` seconds, enriches the pages of new spikes of run `live`. Run it next to `wikipulse.detect.run`; Ctrl+C stops it after the pages in progress. |
| `uv run python -m wikipulse.enrich.run --run-id <run> --since <date> --until <date> --once` | Backfill: enriches the pages of a run's spikes that started in [since, until), then prints the pages, API calls and cache hit rate. `--force` enriches fresh pages again (free while the cache is fresh). |
| `uv run python scripts/enrich_latency.py <run>` | How long after each spike started its page was enriched (the "within a minute" check). |
| `uv run python scripts/make_enrich_fixtures.py` | Re-records the saved API responses in `tests/fixtures/enrich/` used by the unit tests (touches the real APIs). |

### Event grouping

Groups spikes about the same story into events (tables `events` and `event_pages`),
using three signals between spiking pages: links, start times and the meaning of their
summaries (embeddings from the local model `EVENTS_EMBED_MODEL`, downloaded once from
Hugging Face). Weights and thresholds are the `EVENTS_*` settings.

| Command | What it does |
|---------|--------------|
| `uv run python -m wikipulse.events.run` | Live: every `EVENTS_POLL_INTERVAL_S` seconds, groups new spikes of run `live` (waiting up to `EVENTS_ENRICH_WAIT_S` for their pages to be enriched). Run it next to the detector and the enricher. |
| `uv run python -m wikipulse.events.run --spike-run <run> --run-id <name> --since <date> --until <date> --once --reset` | Replay: groups a detector run's spikes of a time range, in order, into grouping run `<name>`, then exits. |
| `uv run python -m wikipulse.events.feed --run-id <name>` | Prints the active events with their pages (`--all` adds ended ones, `--min-pages 2` hides single pages, `--watch 30` refreshes). |
| `uv run python -m wikipulse.eval.grouping label --run-id <name> --day <date>` | Samples page pairs of a day (stratified) and has a local LLM label them as same story / different; `--manual` lets you label them. Saved to `evals/labels/grouping_labels.jsonl`. |
| `uv run python -m wikipulse.eval.grouping report --run-id <name> --day <date>` | Pairwise precision and recall of a grouping run; writes `evals/results/<today>_<name>.md` and `.json`. |

### LLM client

Every LLM call goes through `wikipulse.llm`: `get_client("backend:model")` returns a
client whose `chat()` takes neutral messages, a system prompt, tools, a JSON schema,
temperature, max tokens and a thinking switch. The default is the free local model
`DEFAULT_MODEL=ollama:qwen3:4b` through [Ollama](https://ollama.com) (`ollama pull
qwen3:4b`); `OLLAMA_NUM_CTX`, `OLLAMA_KEEP_ALIVE` and `LLM_TIMEOUT_S` tune it. An
optional `anthropic:<model>` backend needs `uv sync --extra anthropic` and
`ANTHROPIC_API_KEY`.

| Command | What it does |
|---------|--------------|
| `uv run python -m wikipulse.llm.check` | Is Ollama reachable, which models are pulled, and one short test question with its tokens and timing (`--model` checks another model). |
| `uv run pytest -m live tests/llm` | Live tests against the local Ollama: a plain answer, a JSON-schema answer and a tool call. |

## Stage 4: knowledge graph

For each event, the LLM (`EXTRACT_MODEL`, default `ollama:qwen3:4b`) reads its pages'
summaries and the text added during their bursts of edits, and names the people,
organizations, places and events involved and the relationships between them, plus a
title and a one-sentence description. Names are linked to Wikidata (the event's own
pages first, then the articles they link to, then Wikidata search) and everything is
stored in Neo4j with its source page and revision. Neo4j Browser: http://localhost:7474
(user `neo4j`, password `NEO4J_PASSWORD`).

| Command | What it does |
|---------|--------------|
| `uv run python -m wikipulse.graph.run` | Live: every `GRAPH_POLL_INTERVAL_S` seconds, extracts new events of grouping run `live`, and again those whose spikes changed. Failed events are retried later. |
| `uv run python -m wikipulse.graph.run --run-id <name> --since <date> --until <date> --once` | A recorded day: extracts the events of a grouping run that started in [since, until), then prints events, entities, linked share and average time per event. `--limit N`, `--force`, `--reset`, `--model` as in `--help`. |

### Evaluating extraction

| Command | What it does |
|---------|--------------|
| `uv run python -m wikipulse.eval.extraction report --run-id <name> --events <date> --name <run>` | Linking accuracy (against exact Wikidata labels), relation support in Wikidata (a lower bound) and labeled precision, per entity type; writes `evals/results/<today>_<run>.md` and `.json`. `--events` also takes event numbers (`341,356`) or `<start>..<end>`. |
| `uv run python -m wikipulse.eval.extraction label --run-id <name> --events <date> --name <run>` | Has a local LLM (`EVAL_LABEL_MODEL`) judge a fixed sample of 50 extracted relationships against their source text: correct / wrong / unsure; `--manual` lets you label them. Saved to `evals/labels/relation_labels.jsonl`. |
| `uv run python -m wikipulse.eval.extraction extract --run-id <name> --events <date> --name <run> --prompt extract_v2` | Re-extracts the events with another prompt (or `--model`) into `data/extractions/<run>.jsonl`, without touching the graph; `report` and `label` read it with `--from <run>`. |
| `uv run python -m wikipulse.eval.extraction compare <run A> <run B>` | Prints the measures of two reports side by side. |

## Stage 5: questions about the graph (GraphRAG)

Answers questions about current events from the knowledge graph, citing the Wikipedia
pages and revisions every claim comes from. Retrieval combines three channels: graph
traversal around the entities and events the question names, one read-only Cypher query
written by the LLM (`QA_MODEL`, default `ollama:qwen3:8b`) and checked before it runs,
and vector search over page summaries and events. "Today" and "this week" are event
time: relative to the newest event in the graph.

| Command | What it does |
|---------|--------------|
| `uv run python -m wikipulse.qa.index` | Creates the full-text and vector indexes and embeds the pages and events that are new or changed. Run it once, and again after `wikipulse.graph.run` wrote new events. `--force` re-embeds everything. |
| `uv run python -m wikipulse.qa.ask "How are Ueno Bank and Santiago Peña connected?"` | The answer with numbered citations (page, revision, permanent link) and the facts used. `--show-context` adds every retrieved fact, the generated Cypher and search notes; `--channels graph,cypher,vector`, `--as-of`, `--model`, `--json`. |

### MCP server

WikiPulse as an [MCP](https://modelcontextprotocol.io) server, so Claude Desktop or any
other MCP client can ask what is happening and explore the graph. All tools are
read-only (and annotated so): `get_trending_events`, `get_event`, `ask` (the GraphRAG
answer above, slow), `find_connection`, `search_entities`. Resource template:
`event://{event_id}`. Prompts: `daily_briefing`, `explain_event`.

| Command | What it does |
|---------|--------------|
| `uv run python -m wikipulse.mcp_server.server` | Runs the server over stdio (how Claude Desktop and the MCP Inspector start it). |
| `uv run python -m wikipulse.mcp_server.server --transport http` | Streamable HTTP at `http://127.0.0.1:8765/mcp` (`MCP_HTTP_HOST`, `MCP_HTTP_PORT`, or `--host`/`--port`). Loopback addresses only: the server has no authentication. |
| `... --neo4j-uri bolt://localhost:7688` | Serves another graph, e.g. the frozen snapshot of the evaluations. |
| `npx @modelcontextprotocol/inspector uv run python -m wikipulse.mcp_server.server` | Opens the MCP Inspector in the browser, to list and try every tool, resource and prompt (needs Node.js). |

**Claude Desktop.** Open Settings > Developer > Edit Config, which opens
`claude_desktop_config.json` (on Windows in `%APPDATA%\Claude\`), and add the
`wikipulse` entry of [`docs/claude_desktop_config.example.json`](docs/claude_desktop_config.example.json)
to its `mcpServers`, with the path of your clone:

```json
{
  "mcpServers": {
    "wikipulse": {
      "command": "uv",
      "args": [
        "--directory", "C:\\path\\to\\wikipulse",
        "run", "python", "-m", "wikipulse.mcp_server.server"
      ]
    }
  }
}
```

`--directory` makes uv run in the clone, so the server finds `.env`. Restart Claude
Desktop; WikiPulse then appears under the tools icon. The Docker services must be
running, and Ollama for `ask`. If `uv` is not found, give its full path (`where.exe uv`).
The first `ask` takes up to a minute (it loads the embedding model and the LLM).

### Analyst agent

An agent that investigates each significant event through the MCP tools (plus two local
tools, `read_page` and `related_events`) and writes a short briefing in which every
sentence cites the Wikipedia pages and revisions it comes from. The briefing is stored
on the event in Neo4j, so `get_event` shows it; the full transcript of every run goes to
`data/agent_runs/`. The loop (`wikipulse/agent/loop.py`) is written without a framework,
with a step limit, a token budget, per-tool timeouts and loop detection
(`AGENT_*` settings).

| Command | What it does |
|---------|--------------|
| `uv run python -m wikipulse.agent.run` | The trigger: every `AGENT_POLL_INTERVAL_S` seconds, briefs the events whose best spike score reaches `AGENT_MIN_SPIKE_SCORE` or that have at least `AGENT_MIN_PAGES` pages, and that have no up-to-date briefing. Starts the MCP server as a subprocess. `--once`, `--limit N`, `--event KEY` (repeatable), `--dry-run`, `--force`, `--model`, `--neo4j-uri`, `--mcp-url`. |
| `uv run python -m wikipulse.agent.injection` | Prompt-injection tests: replays invented events whose page text carries planted instructions through the agent and checks the briefings don't follow them. Writes a report to `evals/results/`. |

### Web API and live page

A FastAPI backend and one web page (plain HTML, CSS and JavaScript, nothing loaded from
outside) where events appear as soon as they are grouped, grow as pages join, and gain a
headline, entities and a briefing once stages 4 and 5 have processed them. The page also
has a question box (`/ask`, the same GraphRAG answers as `wikipulse.qa.ask`).

| Command | What it does |
|---------|--------------|
| `uv run python -m wikipulse.api` | Starts the API and the page at http://127.0.0.1:8000 for the grouping run `API_RUN_ID` (`live`). `--run-id 03b-default-fix` shows the recorded day; `--port`, `--neo4j-uri`. Loopback only: there is no authentication. |
| `uv run python scripts/demo_live_events.py --reset` | Without a live pipeline: re-groups a recorded day (spike run `02b-loose`, 16:00 to 20:00 UTC) into the run `05e-live-demo` 15 minutes of event time every 10 seconds. Start the API with `--run-id 05e-live-demo` and watch events appear. `--delete` removes the demo run. |

Routes: `GET /events`, `GET /events/{event_id}`, `POST /ask`, `GET /health`, and the
WebSocket `/ws/events` (a snapshot on connect, then every new or changed event). The API
documentation is at http://127.0.0.1:8000/docs (Swagger UI, which loads its scripts from
a CDN; `/openapi.json` works offline).

## Stage 6: data lake

Every kept edit is archived as hourly Parquet files in S3-compatible object storage (the
`rustfs` service of `docker-compose.yml`; web console at http://localhost:9001, login
`S3_ACCESS_KEY` / `S3_SECRET_KEY`): `<S3_BUCKET>/edits/year=YYYY/month=MM/day=DD/hour=HH/part-0.parquet`.

| Command | What it does |
|---------|--------------|
| `uv run python -m wikipulse.lake.archive` | Archiver: reads `wiki:edits` with its own consumer group (`lake:archive`), buffers edits by hour of event time, writes them to the lake every `LAKE_FLUSH_INTERVAL_S` seconds and only then acknowledges them. Ctrl+C writes what is buffered. `--stream-key`, `--group`, `--prefix`, `--exit-when-idle`, `--reset`. |
| `uv run python -m wikipulse.lake.query "<SQL>"` | Ad-hoc SQL (DuckDB) over the archive, which is the table `edits` (with the partition columns `year`, `month`, `day`, `hour`). `--prefix`, `--csv`, `--max-rows`. |
| `uv run python scripts/fill_stream.py --recording <file> --stream-key <key>` | Bulk-loads a recording's kept edits into a stream of your own (much faster than replay), e.g. to archive recorded history. |
| `uv run python scripts/lake_crash_test.py --recording <file>` | Kills the archiver hard three times mid-hour and checks the archive has no gaps or duplicates. |
| `uv run python scripts/lake_size_report.py` | One hour as Parquet vs. the same edits as JSON (sizes and query time); writes `evals/results/<today>_lake-size.md`. |

## Stage 8: Kubernetes

The pipeline runs on a local Kubernetes cluster ([kind](https://kind.sigs.k8s.io/),
installed with `winget install Kubernetes.kind`; `kubectl` comes with Docker Desktop).
One image (`Dockerfile`: multi-stage, uv, non-root) runs every service with a different
command. Redis, Postgres, Neo4j and the S3 store stay in Docker Compose and Ollama on the
host; pods reach them through `host.docker.internal` (`k8s/external-services.yaml`).
Start Compose first (`docker compose up -d`).

| Command | What it does |
|---------|--------------|
| `uv run python scripts/cluster.py up` | Creates the cluster `wikipulse`, builds the image `wikipulse:<commit>`, loads it into kind, creates the Secret from your settings, runs the migrations as a Job and deploys `k8s/` (consumer, detector, archiver, enricher, grouper, graph, api). `create`, `build`, `load`, `secret`, `migrate`, `apply`, `status` run one step; `down` deletes the cluster. |
| `kubectl -n wikipulse get pods` | The pods (kubectl's context is `kind-wikipulse` after `up`). |
| `kubectl -n wikipulse port-forward svc/api 8000:8000` | The live page at http://localhost:8000. |
| `uv run python scripts/k8s_crash_test.py` | Deletes the consumer pod mid-stream (hard, then gracefully), lets Kubernetes restart it, and checks no edit was lost or doubled. About 10 minutes; one Wikimedia connection at a time. |

The cluster's consumer keeps its own checkpoint (`wiki:k8s:consumer:*`) and writes the
shared stream `wiki:edits`: don't run `wikipulse.stream.consume` on the laptop at the
same time. Manifests: `k8s/` (`kubectl apply -k k8s` applies them; `secret.example.yaml`
is only a template). Probes run `python -m wikipulse.health` (workers) or `GET /health`
(API).

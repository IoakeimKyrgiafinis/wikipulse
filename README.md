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
4. Start Redis and Postgres: `docker compose up -d` (check with `docker compose ps`)
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

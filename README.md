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
| `uv run pytest -m integration` | Integration tests against the Docker Redis. |
| `uv run python scripts/crash_test.py` | Five-minute crash test: kills and restarts the consumer, then checks nothing was lost or doubled. |

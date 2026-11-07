# Spec 02a: Spike detector

Status: ready
Stage: 2. Spike detection
Owner: agent
Depends on: 01

## Goal
Detect pages whose editing activity becomes unusual, live, and record each spike with its start, end and strength. Deciding what "unusual" means is the core data science problem of the project, so explain every choice in the learning note.

## What you'll learn
- **Stream processing:** sliding windows, event time, and state that survives restarts, by building them yourself.
- **Anomaly detection on time series:** baselines, cold start, and seasonality (daily rhythms). Read a short introduction to moving averages and z-scores before starting.
- **Redis consumer groups:** how several consumers share one stream, and why acknowledgements matter.
- **Iterating with data:** change one threshold, replay, compare. This loop is the core of applied data science.

## Context
- Read edits from the Redis Stream with a **consumer group** (`XREADGROUP` and `XACK`). That lets several independent consumers read the same stream, and lets yours resume after a restart.
- Follow the event-time rule in AGENTS.md. Then replaying a recording at `--speed max` gives exactly the same spikes as the live run, so you can iterate on your logic in seconds.

## Outputs
- `wikipulse/detect/windows.py`: sliding-window counts per page (for example, edits and distinct editors in the last 10 and 60 minutes)
- `wikipulse/detect/baseline.py`: each page's normal activity level, plus the overall edit rate
- `wikipulse/detect/detector.py`: decides when a spike starts, continues and ends
- `uv run python -m wikipulse.detect.run`: runs detection on the live stream (or on a replay, since both feed the same Redis Stream)
- `uv run python -m wikipulse.detect.trending`: prints the currently active spikes
- Spikes stored in Postgres (see "Also build")
- Config for every threshold you choose

## Requirements
1. Bot edits are excluded.
2. A spike has a lifecycle: started, active, ended (after a quiet period), so one burst doesn't produce many separate spikes.
3. Each spike records: title, start time, end time, total edits, distinct editors, peak edits per 10 minutes, and a score.
4. Detection state survives restarts (for example, snapshotted to Redis every minute).
5. Same input gives the same output (determinism).

## Acceptance criteria
- Replaying the same recording twice produces identical spikes.
- Unit tests for the window counting, baseline and lifecycle logic.
- `trending` shows sensible pages during a live run.

## Also build
- The Postgres table `spikes`, with a small migration system: plain SQL files applied in order, tracked in a `schema_migrations` table, run with `uv run python -m wikipulse.db.migrate`.
- `wikipulse/db/spikes.py` to insert, update and query spikes, with integration tests.
- Tests for `wikipulse/detect/` using replays of `tests/fixtures/stream_sample.jsonl`.

## Design notes
Things worth deciding deliberately:
- **Cold start:** most pages have almost no history. A page going from 0 to 3 edits isn't news, so you'll need minimum thresholds as well as "times above normal."
- **Distinct editors:** one person making 20 edits in 10 minutes is someone working on an article, not breaking news. Many *different* people editing is the stronger signal.
- **Daily rhythm:** Wikipedia's overall edit rate rises and falls through the day. Comparing a page to the overall rate helps.
- **Edit wars and vandalism:** a vandal plus reverts creates a burst of edits. Comments containing "Reverted" or "Undid" are a clue. Decide whether those count.
- Start simple (fixed thresholds). Spec 02b measures it; later improvements are judged by those numbers.

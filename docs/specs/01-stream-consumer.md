# Spec 01: Live stream consumer

Status: ready
Stage: 1. Live stream consumer
Owner: agent
Depends on: 00

## Goal
A crash-safe consumer that reads Wikipedia's live edit stream, keeps English Wikipedia article edits, normalizes them, and writes them into a Redis Stream, plus a recorder and a replayer so the whole pipeline can be tested on real data without the network.

## What you'll learn
- **Streaming and Server-Sent Events:** open the stream URL in your browser for a few seconds before the agent starts, and look at raw events.
- **Delivery guarantees:** at-least-once delivery, checkpoints and idempotency. Read a short article on "exactly-once vs at-least-once" first; the crash test then shows you the theory working.
- **Redis Streams:** run `XLEN`, `XRANGE` and `XINFO STREAM` yourself in `redis-cli` (via `docker compose exec`).
- **Testability by design:** why the processing core doesn't know whether events come from the network or a file. Explain this in your devlog.
- **Async Python and resilient clients:** reconnects, backoff with jitter, graceful shutdown.

## Context
- Source: Wikimedia EventStreams, `https://stream.wikimedia.org/v2/stream/recentchange`, using Server-Sent Events (SSE). Each message has an SSE `id` and a JSON `data` payload. Schema: the `mediawiki/recentchange` schema linked from https://stream.wikimedia.org/?doc
- **Resuming:** send the last received SSE `id` as the `Last-Event-ID` request header to continue where you left off. The service also accepts a `since` URL parameter (a timestamp) for consuming recent history, within a limited retention window.
- **Canary events:** the stream contains test events whose `meta.domain` is `canary`. Discard them.
- The public service is meant for small-scale tools: one connection, a descriptive User-Agent (from config), and polite reconnect behavior.
- Keep bot edits, flagged as bots. Later stages filter them, and their share is useful data.

## Outputs
- `wikipulse/stream/models.py`: an `EditEvent` pydantic model: `event_id` (from `meta.id`), `event_time` (from `meta.dt`), `received_at`, `title`, `type`, `user`, `is_bot`, `is_minor`, `rev_old`, `rev_new`, `len_old`, `len_new`, `comment` (truncated to 500 characters)
- `wikipulse/stream/filters.py`: `is_wanted(raw: dict) -> bool` and `normalize(raw: dict) -> EditEvent`
- `wikipulse/stream/sources.py`: a `Source` interface yielding `(sse_id, raw_json)` pairs, with `LiveSource` (SSE over HTTP) and `FileSource` (a recording file)
- `wikipulse/stream/consumer.py`: the processing core, independent of the source: filter, normalize, deduplicate, write to Redis, save the checkpoint
- CLIs:
  - `uv run python -m wikipulse.stream.consume`: the live consumer
  - `uv run python -m wikipulse.stream.record --minutes 60 [--since <ISO timestamp>]`: saves *raw, unfiltered* messages (SSE id and data) to `data/recordings/<start time>.jsonl`
  - `uv run python -m wikipulse.stream.replay --file <path> [--speed 10|max]`: feeds a recording through the same processing core
  - `uv run python -m wikipulse.stream.stats [--watch]`: prints live statistics
- Tests in `tests/stream/`, and a fixture `tests/fixtures/stream_sample.jsonl` (about 300 raw messages, cut from a real recording)
- Config additions: `STREAM_URL`, `STREAM_KEY=wiki:edits`, `STREAM_MAXLEN=1000000`, `RECONNECT_MAX_S=60`

## Requirements
1. **Filter:** keep events where `wiki == "enwiki"`, `namespace == 0`, and `type` is `edit` or `new`. Discard canary events.
2. **Write:** each kept event goes to the Redis Stream `STREAM_KEY` with `XADD`, trimmed approximately to `STREAM_MAXLEN` so memory stays bounded. Store the event as JSON in a `data` field, plus `title` and `event_time` as separate fields.
3. **Checkpoint:** after each write, save the SSE `id` in Redis (`wiki:consumer:last_id`), in the same transaction as the write. On start, resume from it with `Last-Event-ID`.
4. **Deduplication:** resuming can resend events. Skip any `event_id` already written in the last hour (Redis `SET NX` with expiry, in the same transaction).
5. **Reconnect:** on disconnect or error, reconnect with exponential backoff and jitter, up to `RECONNECT_MAX_S`. Log each reconnect and count it.
6. **Graceful shutdown:** Ctrl+C finishes the current write, saves the checkpoint and exits cleanly.
7. **Statistics** in a Redis hash `wiki:consumer:stats`: events received, kept, discarded, duplicates skipped, reconnects, last event time. `stats` shows events per second over the last minute, stream length, and lag (now minus the newest `event_time`). `--watch` refreshes every 2 seconds.
8. **Malformed messages** are logged and counted, never crash the consumer.
9. **Replay** uses exactly the same processing core as live consumption. `--speed max` processes as fast as possible; `--speed N` replays N times faster than real time, based on event timestamps.

## Acceptance criteria
- Unit tests pass, using the fixture and fake sources: filtering, canary handling, normalization, malformed messages, deduplication, checkpoint saving, reconnect after a fake source raises mid-stream, and graceful shutdown.
- Integration tests (`-m integration`, using the Docker Redis) pass: writes, trimming, and resuming from a checkpoint.
- The GitHub Actions workflow runs the integration tests too, with Redis as a service container.
- **Crash test,** run by you and described in your summary: run the recorder and the consumer at the same time; kill the consumer after 2 minutes, restart it 1 minute later, and stop both after 5 minutes. The set of `event_id`s in Redis for that period must equal the filtered set from the recording: nothing missing, nothing duplicated. Put this in `scripts/crash_test.py` so it can be re-run.
- `stats --watch` shows a live event rate. Report the typical events per second and the share of bot edits.

## Look at it yourself (optional, never blocking)
- Watch `stats --watch` for a few minutes and run the crash test yourself.
- Read `consumer.py` closely: checkpointing and deduplication are the core ideas of this stage.

## Out of scope
- Spike detection, Postgres tables, Prometheus metrics
- Other wikis or streams

## Notes for the agent
- Add `httpx` and `httpx-sse` for the async SSE client, or another well-maintained SSE client if you have a good reason (explain it).
- Building the fixture: record 2 minutes, then keep a mix of about 300 messages, including some to be filtered out (other wikis, other namespaces, a canary event if one appears).

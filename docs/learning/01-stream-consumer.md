# Learning note: Spec 01, Live stream consumer

## What was built

A crash-safe pipeline from Wikipedia's live edit stream into Redis, plus tools to record
the raw stream and replay it offline.

| File | Role |
|------|------|
| `wikipulse/stream/models.py` | `EditEvent`, the normalized edit (pydantic, immutable). Truncates `comment` to 500 characters. |
| `wikipulse/stream/filters.py` | `is_wanted(raw)` (enwiki, namespace 0, `edit`/`new`, no canaries) and `normalize(raw)`. |
| `wikipulse/stream/sources.py` | The `Source` interface yielding `(sse_id, raw_json)`, with `LiveSource` (SSE over HTTP with httpx + httpx-sse) and `FileSource` (a recording, optionally paced by event time). Also the recording line format. |
| `wikipulse/stream/consumer.py` | The processing core, `Consumer`: parse, filter, normalize, deduplicate, `XADD`, checkpoint, stats. `run_live()` adds resume-from-checkpoint and reconnects. |
| `wikipulse/stream/resilience.py` | `Backoff` (exponential with jitter), `run_with_reconnect()`, `until_stop()` (stoppable iteration), `install_stop_handlers()` (Ctrl+C). |
| `wikipulse/stream/consume.py`, `record.py`, `replay.py`, `stats.py` | The four command-line tools. |
| `scripts/crash_test.py` | The re-runnable crash test. |
| `scripts/make_fixture.py` | Cuts `tests/fixtures/stream_sample.jsonl` (290 real messages) from a recording. |
| `tests/stream/` | 69 unit tests (no network, no Docker) and 4 integration tests (`-m integration`). |

How data flows:

```
LiveSource (HTTP/SSE) ─┐
                       ├─> (sse_id, raw_json) ─> Consumer.process() ─> Lua script in Redis:
FileSource (.jsonl) ───┘                          parse, is_wanted,     SET seen NX EX, XADD,
                                                  normalize             SET checkpoint, HINCRBY stats
```

`record` writes the raw `(sse_id, data)` of *every* message to a file. `replay` reads that
file with `FileSource` and feeds it to the same `Consumer`. `stats` reads the stats hash and
the stream.

Redis keys (live consumer):

| Key | Type | Content |
|-----|------|---------|
| `wiki:edits` (`STREAM_KEY`) | Stream | one entry per kept edit: fields `data` (the `EditEvent` as JSON), `title`, `event_time` |
| `wiki:consumer:last_id` | String | the SSE id of the last processed message (the checkpoint) |
| `wiki:consumer:seen:<event_id>` | String with 1 h expiry | deduplication marker |
| `wiki:consumer:stats` | Hash | `received`, `kept`, `kept_bots`, `discarded`, `duplicates`, `malformed`, `reconnects`, `last_event_time` |

Replay uses `wiki:replay:*` for its checkpoint and stats, so it never moves the live checkpoint.

## Key concepts

### Streaming and Server-Sent Events

SSE is a plain long-lived HTTP response with `Content-Type: text/event-stream`. The server
keeps writing small text blocks separated by blank lines:

```
event: message
id: [{"topic":"eqiad.mediawiki.recentchange","partition":0,"offset":-1},{"topic":"codfw.mediawiki.recentchange","partition":0,"timestamp":1794014237668}]
data: {"$schema":"/mediawiki/recentchange/1.0.0","meta":{"id":"c378...","dt":"2026-11-07T01:17:17.667Z",...},...}
```

`LiveSource.messages()` opens it with `httpx_sse.aconnect_sse` and yields `(sse.id, sse.data)`
for every `message` event. Lines starting with `:` (like `:ok`) are comments that keep the
connection alive; httpx-sse skips them.

Note the `id`: Wikimedia's SSE ids are JSON lists with one position per Kafka topic, and here
the position is a **timestamp**, not a unique counter. In the 2-minute recording 47 ids
appeared twice. That is why resuming can re-send events, and why deduplication matters.

### Delivery guarantees: at-least-once, checkpoints and idempotency

- *At-most-once*: save your position before processing. A crash loses the message.
- *At-least-once*: save your position after processing. A crash may process a message twice.
- *Exactly-once*: in practice, at-least-once delivery plus an *idempotent* write, so doing it
  twice has the same effect as doing it once.

`Consumer._write()` runs one Lua script (`_WRITE_SCRIPT`). Redis executes a script as a single
atomic step, so these happen all together or not at all:

1. `SET wiki:consumer:seen:<event_id> 1 NX EX 3600`: succeeds only if the id is new,
2. `XADD wiki:edits MAXLEN ~ 1000000 * data ... title ... event_time ...` (only if step 1 succeeded),
3. `SET wiki:consumer:last_id <sse id>`,
4. `HINCRBY` on the stats counters.

Messages we don't write (discarded, malformed) still move the checkpoint, through a
`MULTI`/`EXEC` transaction in `Consumer._skip()`.

Why it is safe: if the process dies before the script, nothing happened and the checkpoint still
points to the previous message, so the server re-sends this one after the restart. If it dies
after the script, the checkpoint already includes it. And whatever the server re-sends anyway
(timestamp-based ids) fails step 1 and is counted as `duplicates`. The crash test shows this.

### Redis Streams

A Redis Stream is an append-only log. Each entry has an id like `1794014244928-0`
(milliseconds since 1970, then a sequence number) and a few field/value pairs.
`MAXLEN ~ N` trims the oldest entries; the `~` lets Redis trim only whole internal nodes, which
is much cheaper, so the length hovers slightly above N (the integration test
`test_stream_is_trimmed_approximately` checks `maxlen <= length <= maxlen + 100`).
Stage 2 reads this stream with a consumer group.

Try it (PowerShell, in the repository folder):

```
docker compose exec redis redis-cli XLEN wiki:edits
docker compose exec redis redis-cli XRANGE wiki:edits - + COUNT 2
docker compose exec redis redis-cli XINFO STREAM wiki:edits
docker compose exec redis redis-cli HGETALL wiki:consumer:stats
docker compose exec redis redis-cli GET wiki:consumer:last_id
```

`stats` uses the fact that entry ids start with the insertion time: `XRANGE wiki:edits
<now-60s in ms> +` returns exactly the entries added in the last minute.

### Testability by design

`Consumer` never opens a network connection. It takes `(sse_id, raw_json)` pairs from any object
with a `messages(last_event_id)` method (the `Source` protocol). In production that is
`LiveSource`; in replay it is `FileSource`; in tests it is `FakeSource` (`tests/stream/helpers.py`),
which can fail on purpose after N messages or go quiet forever. Redis is replaced in unit tests
by `fakeredis`, which runs the real Lua script, so the same code path is tested without Docker.
The result: the reconnect, crash/resume and shutdown behavior is tested in about a second,
deterministically, and replaying a recording gives the same stream entries as the live run.

### Async Python and resilient clients

- **Reconnects.** `run_with_reconnect()` calls an `attempt` (one connection) in a loop. When the
  attempt raises or the server closes the stream, it waits and tries again, counting each
  reconnect in `stats`. Each attempt in `Consumer.run_live()` reloads the checkpoint from Redis,
  so even a failed Redis write is handled the same way: reconnect and re-read from the checkpoint.
- **Backoff with jitter.** `Backoff.next_delay()` doubles the wait each time (1, 2, 4, ... up to
  `RECONNECT_MAX_S` = 60 s) and picks a random value between half and all of it. Doubling avoids
  hammering a struggling server; the randomness stops many clients from retrying in lockstep.
  After a successful message `backoff.reset()` starts again from 1 s.
- **Graceful shutdown.** `install_stop_handlers()` turns Ctrl+C (SIGINT, and SIGTERM/SIGBREAK)
  into setting an `asyncio.Event`. `until_stop()` runs the source in its own reader task that
  fills a one-slot queue, and waits for *either* the next message *or* the stop event. So a stop
  interrupts waiting for the network immediately, but never a message that is being written.
  A second Ctrl+C raises `KeyboardInterrupt` as an escape hatch.

## Design decisions and deviations

- **Lua script instead of `MULTI`/`EXEC` for the write.** The spec asks for the dedup check, the
  `XADD` and the checkpoint "in the same transaction". `MULTI`/`EXEC` can't make the `XADD`
  depend on the result of `SET NX` inside the transaction; a Lua script can, and it is atomic.
- **The checkpoint also advances on discarded and malformed messages.** Otherwise a restart
  would re-read everything since the last *kept* message, and a malformed message could be
  re-read forever.
- **`normalize(raw, received_at=None)`.** The spec's signature is `normalize(raw)`; the optional
  `received_at` lets the consumer (and tests) pass the time explicitly. `received_at` is
  wall-clock metadata only; all processing logic uses `event_time`.
- **Extra stats counters**: `malformed` (requirement 8 says they're counted) and `kept_bots`
  (for the bot share). `received = kept + discarded + duplicates + malformed`.
- **Key namespaces.** `consume` and `stats` accept `--stream-key` and `--namespace`, and replay
  uses `wiki:replay:*` with fresh dedup keys per run (so replaying a file twice writes it twice,
  which stage 2 needs for its determinism check). The crash test uses `wiki:crashtest:*`, so it
  can run next to a live consumer.
- **`consume --since`**: used only when there is no checkpoint yet. The crash test needs it so
  the consumer and the recorder start from the same point of the stream.
- **Recording format**: one JSON object per line: `{"id": <SSE id>, "data": <raw payload string>,
  "received_at": <ISO time>}`. `data` stays the exact unparsed string, so even malformed
  payloads are replayed as they came. A broken line (e.g. cut off by a crash) is replayed as a
  malformed message instead of being dropped.
- **Replay pacing** (`--speed N`) compares each event's `meta.dt` with the first event's and
  sleeps until it is due, so sleep inaccuracies don't add up; out-of-order events don't wait.
- **`fakeredis[lua]` as a dev dependency** (not mentioned in the spec). It lets unit tests run
  the real Lua script in memory, so deduplication and checkpoints are unit-tested without
  Docker, as the spec's acceptance criteria ask. The integration tests repeat the key cases on
  the real Redis. Runtime dependencies added: `httpx` and `httpx-sse`, as the spec suggests.
- **`tests/__init__.py` and `tests/stream/__init__.py`** were added so test modules can import
  the shared helpers in `tests/stream/helpers.py`.
- **Fixture**: 290 messages cut by `scripts/make_fixture.py` from a 2-minute recording (150 kept,
  140 filtered: other wikis, other namespaces, `log`/`categorize` types). No canary event
  appeared in that recording, so the canary test marks a real message as `canary` instead.
- **Live data left in Redis**: the 3-minute check left 303 entries in `wiki:edits` and a
  checkpoint in `wiki:consumer:last_id`. The next `consume` resumes from that checkpoint (the
  server keeps a few days of history). Delete both keys for a fresh start.
- **Crash test timing**: the recorder runs 30 s longer than the consumer (5.5 instead of 5
  minutes), so every event the consumer wrote is guaranteed to be in the recording. The
  comparison covers the recording up to the consumer's last written event. The crash test
  holds two stream connections (recorder and consumer) at once, which the test inherently needs.
- **User-Agent**: `.env` still has the placeholder contact `you@example.com`. The short live
  runs (recording, crash tests, 3-minute stats check) used it, and every CLI logs a warning about it.
  **Set your real contact in `.env` before running the consumer for long.**
- **CI**: the workflow now starts a `redis:7-alpine` service container and runs
  `uv run pytest -m integration` after the unit tests. It was not validated with actionlint
  this time (running it was not permitted in this session); it runs on the next push.

## Results

All measured on 2026-11-07 around 01:20 to 01:40 UTC (night in the Americas, so rates are
lower than at peak hours).

- **Live rate (`stats --watch`, 3 minutes of the live consumer):** about **32 to 39 messages/s**
  arrive from the whole stream (all wikis), and about **1.7 kept events/s** (English Wikipedia
  articles; refreshes ranged from 0.65 while warming up to 2.5). The consumer kept 303 of 5,787
  messages (5.2%). Lag (now minus newest `event_time`) stayed under 1 s while running.
- **Bot share of kept edits:** **31%** over the 3-minute live run (28 to 36% per minute), and
  36% (66 of 181) in the 2-minute fixture recording. Bots are kept and flagged (`is_bot`).
- **Crash test (`uv run python scripts/crash_test.py`, full 5-minute timeline):** consumer killed
  hard at 2:00, restarted at 3:00, stopped with Ctrl+Break at 5:00 (exit code 0).
  487 kept events in the recording, 487 entries in Redis, **0 missing, 0 extra, 0 duplicates**.
  87 kept events arrived while the consumer was dead, and all 87 were recovered after the restart
  (resume from the checkpoint via `Last-Event-ID`). PASS. A second run at `--scale 0.2` after a
  small logging change also passed (95/95, 20 recovered).
- **Replay** of the 2-minute recording (3,172 messages): `--speed max` took about 3 s (roughly
  1,000 messages/s, including process start-up); `--speed 10` took about 13 s, as expected for
  2 minutes at 10x. Both produced the same counts as the live filter (181 kept, 2,991 discarded).
- **Tests:** 69 unit tests pass (`uv run pytest`), 4 integration tests pass
  (`uv run pytest -m integration` against the Docker Redis). `ruff check` and
  `ruff format --check` are clean.

## Check your understanding

1. The consumer is killed with `TerminateProcess` right after Redis ran the Lua script for
   message M but before Python received the reply. What happens on restart?
2. Why does the consumer need deduplication at all, if it always resumes from the exact SSE id
   of the last message it processed?
3. `stats` computes "kept events per second" from stream entry ids. Why would that number be
   misleading during `replay --speed max`, and which number is not affected?

<details>
<summary>Answers</summary>

1. Nothing is lost or doubled. The script ran atomically, so the event is in the stream *and*
   the checkpoint already points at M. The restart resumes after M. Had the kill happened before
   the script ran, the checkpoint would point at the message before M, so M would be sent again
   and written once.
2. Wikimedia's SSE ids are timestamp positions, not unique counters (several messages can share
   one millisecond), so resuming from an id can re-send messages we already have. A crash in a
   less careful design, or a reconnect through another datacenter, can also re-send. The
   `SET ... NX` marker makes writing the same event twice harmless.
3. Entry ids record when the entry was *inserted* (wall clock). A replay at max speed inserts an
   hour of events in seconds, so the rate looks huge. The `event_time` field inside each entry is
   the event's own timestamp and is the same for live and replayed data, which is why stage 2
   works on event time.

</details>

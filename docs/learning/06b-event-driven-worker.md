# Learning note: Spec 06b, Event-driven worker

## What was built

When a new hourly Parquet file lands in the lake, the object store announces it, and a
worker reacts by computing that hour's statistics into Postgres. Nothing polls.

| File | Role |
|------|------|
| `wikipulse/workers/setup.py` | `uv run python -m wikipulse.workers.setup`: the set-up script. Turns on RustFS's notification module, defines a Redis notification *target*, and gives the bucket a notification configuration ("ObjectCreated of `*.parquet` -> that target"). Idempotent; `--show` prints the bucket's configuration. |
| `wikipulse/workers/s3_api.py` | `sign_headers()` (AWS Signature Version 4, by hand) and `S3Api`, a tiny signed HTTP client for the two calls pyarrow can't make. |
| `wikipulse/workers/notifications.py` | `ObjectRef` (bucket, key, event name, size, ...) and `parse_message()`: S3 event JSON -> `ObjectRef`s, URL-decoded keys, non-"created" events dropped. |
| `wikipulse/workers/hourly_stats.py` | The handler. `handle(event, context)` finds the hour the object belongs to (`locate()`), reads *all* files of that hour (`hour_files()`, `read_files()`), computes `compute_counts()` and upserts one row. Stateless. |
| `wikipulse/workers/invoker.py` | `Invoker`: calls the handler, retries with doubling delays, and after the last failed attempt pushes a `DeadLetter` record onto a Redis list. `dead_letters()` and `redrive()`. |
| `wikipulse/workers/listener.py` | `NotificationListener`: subscribes to the Redis channel, runs the catch-up, then invokes the handler for each event of its bucket; resubscribes (and catches up again) after a lost connection. |
| `wikipulse/workers/backfill.py` | `lake_hours()` lists every hour of the bucket; `backfill()` invokes the handler for hours with no row, or with a row computed from other files. |
| `wikipulse/workers/run.py` | `uv run python -m wikipulse.workers.run [listen\|backfill\|dead-letters\|redrive]`. |
| `wikipulse/db/migrations/0006_create_hourly_stats.sql`, `wikipulse/db/hourly_stats.py` | Table `hourly_stats`, the idempotent upsert, `PostgresStats` (reconnects after a broken connection). |
| `wikipulse/config.py`, `.env.example` | `LAKE_NOTIFY_CHANNEL`, `LAKE_NOTIFY_REDIS_URL`, `LAKE_NOTIFY_TARGET`, `WORKER_MAX_ATTEMPTS`, `WORKER_RETRY_DELAY_S`, `WORKER_DEAD_LETTER_KEY`, `WORKER_TOP_PAGES`. |
| `.github/workflows/ci.yml` | `LAKE_NOTIFY_REDIS_URL=redis://redis:6379/0`, so the RustFS service container can reach the Redis service container. |
| `tests/workers/` | 42 unit tests (local folder as the lake, fakeredis, an in-memory stats table, a fake RustFS over `httpx.MockTransport`) and 4 integration tests against the real RustFS, Redis and Postgres. |

How it fits together:

```
lake archiver (06a)                          RustFS (S3)
  write_hour() ─── PUT edits/year=.../hour=13/part-0.parquet ──> bucket wikipulse-lake
                                                                   │ notification config:
                                                                   │ ObjectCreated, *.parquet
                                                                   ▼
                                                     target "wikipulse" (notify_redis)
                                                                   │ PUBLISH
                                                                   ▼
                                               Redis pub/sub channel lake:notifications
                                                                   │
  uv run python -m wikipulse.workers.run                           ▼
    NotificationListener ── parse_message() ──> ObjectRef(bucket, key, ...)
         │ 1. subscribe  2. catch-up (backfill)  3. one event at a time
         ▼
    Invoker.invoke(event) ── handle(event, context) ──> read the hour's files from S3
         │   failed? wait 2 s, 4 s, ... (WORKER_MAX_ATTEMPTS in all)   compute_counts()
         │   still failing? LPUSH workers:hourly_stats:dead            upsert hourly_stats
         ▼
    Postgres hourly_stats (dataset, hour) -> edits, distinct pages/editors, bot share,
                                             top pages, source files/size/modified
```

Run it:

```
uv run python -m wikipulse.db.migrate           # creates hourly_stats
uv run python -m wikipulse.workers.setup        # once (safe to repeat)
uv run python -m wikipulse.workers.run          # listen (Ctrl+C to stop)
uv run python -m wikipulse.workers.run backfill # or only catch up, then exit
uv run python -m wikipulse.workers.run dead-letters
uv run python -m wikipulse.workers.run redrive
```

## Key concepts

### Event-driven architecture

Earlier stages *poll*: the enricher (03a), the grouper (03b), the graph runner (04b) and
the agent trigger (05d) ask Postgres "anything new?" every few seconds. Polling is
simple, but it costs work when nothing happens and adds up to one poll interval of delay.

Here the producer *announces* what happened and consumers react. The archiver doesn't
know the worker exists: it writes a file, the object store turns the write into an
event ("object created: `edits/.../hour=13/part-0.parquet`"), and anyone subscribed
reacts. Producer and consumer are *decoupled*: you can add a second consumer (a
thumbnail generator, a data-quality check) without touching the archiver, and the
worker runs only when there is something to do. In the demo run below the row appeared
within about a second of the write.

The price is that events can be lost or repeated, and the design has to say what
happens then. That is the rest of this note.

**Pub/sub is fire-and-forget.** RustFS's Redis target uses `PUBLISH` (checked in its
source, `crates/targets/src/target/redis.rs`). A pub/sub message reaches the clients
subscribed *at that moment* and is gone; Redis keeps no copy. RustFS can buffer events
when Redis itself is unreachable, but not when Redis is up and nobody listens. So
`NotificationListener.run()` does three things in this order:

1. **subscribe**,
2. **catch up** (`backfill()`): handle every hour whose row is missing or stale,
3. handle messages as they arrive.

Subscribing *before* catching up is the important detail: a file written during the
catch-up has its message waiting in the subscription, so there is no gap. It may be
handled twice (by the catch-up and by its message), which is harmless, see idempotence.
After a lost Redis connection the listener subscribes again and repeats the catch-up
(`test_lost_connection_resubscribes_and_catches_up_again`).

The catch-up doesn't trust notifications at all: it compares the lake with the table.
`hourly_stats` stores `source_size` and `source_modified` (the files the numbers came
from); `backfill()` lists the bucket (`lake_hours()`) and re-handles any hour where
those differ or where there is no row. That is requirement 3, and it also covers files
written before the notifications were set up, and a worker killed mid-invocation.

### The serverless pattern, and how it maps to AWS Lambda

A serverless function is a small piece of code that is *invoked with one event* and
keeps *no state* between invocations. The platform decides when, where and how many
copies run. `wikipulse/workers/hourly_stats.py` is written in that shape:

- `handle(event: ObjectRef, context: HandlerContext) -> HandlerResult` is the
  equivalent of Lambda's `def handler(event, context)`.
- `HandlerContext` holds the clients (pyarrow's S3 filesystem, the Postgres sink),
  built once by `build_worker()` and reused by every invocation, like the code a Lambda
  runs *outside* its handler (database connections, SDK clients) to reuse them across
  warm invocations.
- Everything the handler knows comes from the event and the files; all results go to
  Postgres. Two workers, a restart, or a completely new process behave the same.

The mapping to AWS, piece by piece:

| Here | On AWS |
|------|--------|
| RustFS bucket `wikipulse-lake` | S3 bucket |
| `PutBucketNotificationConfiguration` with a `QueueConfiguration` (`setup.put_bucket_notification`) | the same API call, with a `LambdaFunctionConfiguration` (or SQS/SNS) |
| target `arn:rustfs:sqs:us-east-1:wikipulse:redis` | the Lambda function's ARN (plus a resource policy allowing S3 to invoke it) |
| Redis pub/sub + `NotificationListener` | Lambda's internal event queue for *asynchronous* invocations |
| `Invoker`: 3 attempts in all, waits 2 s, 4 s | Lambda async retries: 2 retries (3 attempts), with backoff |
| `workers:hourly_stats:dead` (Redis list) | a dead-letter queue (SQS) or an "on-failure destination" |
| `run.py redrive` | SQS "start DLQ redrive" |
| `run.py backfill` | an S3 Inventory report or a one-off listing job |

The notification JSON is the same format AWS uses (`Records[].s3.object.key`,
URL-encoded), so `parse_message()` would read a real S3 event unchanged.

### Idempotent handlers

Delivery can repeat: the catch-up and a message may cover the same hour, the archiver
rewrites each hour about every 5 minutes (each rewrite is a new notification), and
`redrive` sends events again. The handler is safe against all of it because it never
*adds*:

- it reads **all** files of the hour (not "the rows in the new file") and computes the
  numbers from scratch (`compute_counts()`);
- it **upserts** by key, `INSERT ... ON CONFLICT (dataset, hour) DO UPDATE`
  (`wikipulse/db/hourly_stats.py`), so the second write replaces the first with the same
  values.

A handler that did `UPDATE hourly_stats SET edits = edits + <rows in file>` would count
an hour twice on every repeated delivery; the 06a archiver rewrites the whole hour file,
so it would even count rows that were already counted.
`test_same_notification_twice_does_not_double_count` and the listener test (the same
message published twice gives one row with 2 edits, not 4) check this.

One more ordering detail: if two notifications of the same hour were handled out of
order (two workers in parallel, or a slow retry), the older files must not overwrite
numbers from newer ones. The upsert's `WHERE hourly_stats.source_modified <=
EXCLUDED.source_modified` keeps the newer row (`test_upsert_is_idempotent_and_keeps_newer_files`).

### Retries and the dead-letter list

Some failures are temporary (Postgres restarting, S3 timing out), some permanent (a
corrupt file). `Invoker.invoke()` can't tell them apart, so it does what Lambda does:
retry a few times with growing waits, then give up *visibly*. A given-up event goes to
the dead-letter list as JSON with the event, the number of attempts, the last error and
the time. Nothing is lost silently and nothing is retried forever (which would block the
events behind it). After fixing the cause, `redrive` takes each record off the list
and invokes it once more; one that fails again goes back onto the list, so a redrive
always ends.

Failures that retrying can't fix *and* that aren't errors are skipped instead: an
object that isn't an hourly data file, or an hour whose files were deleted by the time
the handler ran.

## Design decisions and deviations from the spec

- **RustFS instead of MinIO** (as in 06a), configured through **RustFS's admin API**.
  RustFS supports notification targets for webhook, MQTT, Redis, Kafka, NATS, AMQP,
  Postgres and others. Targets can be defined by environment variables in Compose, but
  the spec asks for a script, and an environment-defined target can't be changed through
  the API afterwards. So `setup.py` uses `PUT /rustfs/admin/v3/module-switches`
  (RustFS ships with notifications off), `PUT /rustfs/admin/v3/target/notify_redis/
  wikipulse`, then waits for the target's ARN (`GET .../target/arns` lists only targets
  that are online, i.e. that reached Redis). These endpoints are RustFS-specific; I found
  them in RustFS 1.0.4's source (`rustfs/src/admin/handlers/event.rs`,
  `module_switch.rs`). The bucket part is the standard S3 call. The settings are
  persisted in RustFS's data volume, so running the script once is enough.
- **Redis, not a webhook.** Redis is already there and needs nothing listening on the
  host; a webhook would need an HTTP server that the RustFS *container* can reach
  (`host.docker.internal` locally, special networking in CI). The cost is pub/sub's
  fire-and-forget delivery, which the subscribe-then-catch-up design covers.
- **`LAKE_NOTIFY_REDIS_URL` is the address RustFS uses**, `redis://redis:6379/0`: inside
  the RustFS container `localhost` is RustFS itself, and Compose's network resolves the
  service name `redis`. CI uses the same name. This is the one CI detail I could not
  check locally: GitHub Actions attaches service containers to one network under their
  service names, so it should work; if the CI run shows the target as "not online",
  this is the place to look.
- **SigV4 by hand, no new dependency.** pyarrow can't set bucket notifications, and
  boto3 isn't allowed by the spec. Signing is about 40 lines (`s3_api.sign_headers()`),
  and the unit test checks it against two signatures published in the AWS S3
  documentation. httpx was already a dependency.
- **The setup script owns the bucket's notification configuration**: S3 replaces it as
  a whole, so running `setup` overwrites any other rules on that bucket. There were none.
- **Primary key `(dataset, hour)`, not just `hour`.** The bucket holds the live archive
  (`edits/`) and recorded history (`history/edits/`, from 06a), which overlap in time;
  tests write their own prefixes too. `dataset` is `<bucket>/<prefix>`, e.g.
  `wikipulse-lake/edits`. Still "upsert by hour", per archive. Spec 06c's daily
  statistics should filter on `dataset = 'wikipulse-lake/edits'`.
- **The handler reads every file of the hour**, not only the object in the event. 06a
  writes one `part-0.parquet` per hour, but the layout allows `part-<n>`; computing the
  hour from all its files keeps the numbers right either way and is what makes the
  handler idempotent.
- **Columns.** The spec's five statistics, plus `bot_edits` next to `bot_share`, and per
  top page its edits *and* distinct editors (`[{"title", "edits", "editors"}]`), because
  02a showed that many edits by one editor is not news. Top pages are all edits, bots
  included; ties are broken by editors, then title, so the list is deterministic.
  `WORKER_TOP_PAGES` (10) sets the length.
- **One bucket per worker; all prefixes.** The worker handles events of `S3_BUCKET`
  (`--bucket` to change) and ignores other buckets' events on the same channel. The
  backfill covers the whole bucket by default (`--prefix` narrows it).
- **Event time vs. wall clock.** The hour comes from the partition (event time, as 06a
  wrote it). Only metadata uses the clock: `computed_at`, `failed_at`, and the retry
  waits.
- **Retries in-process.** The invoker sleeps between attempts and handles events one at
  a time. At one notification per hour file per archiver flush (every 5 minutes) that is
  plenty. A worker killed during the retries loses that event, and the next catch-up
  recomputes the hour.
- **Migrations aren't applied by the worker** (like every other runner here); run
  `uv run python -m wikipulse.db.migrate` first.
- **Not committed.** The instructions for this run were to leave committing to the
  human, so all changes are in the working tree of branch `spec-06b-event-driven-worker`.

## Results

All on 2026-11-09, on the human's Windows machine with Docker's RustFS, Redis and Postgres.

### Acceptance criterion 1: unit tests for the handler; writing a file triggers the stats row

- Unit tests (`uv run pytest tests/workers`): 42 passed. `test_hourly_stats.py` (14):
  numbers on a hand-made hour (8 edits, 4 pages, 6 editors, 25% bots, tie-broken top
  pages), an empty hour, row-order independence, key parsing, same notification twice,
  a rewritten hour, two part files, skipped objects, a corrupt file raising. Plus
  `test_invoker.py` (7), `test_backfill.py` (5), `test_listener.py` (3),
  `test_notifications.py` (8), `test_s3_api_and_setup.py` (5).
- Integration (`uv run pytest -m integration tests/workers`): 4 passed.
  `test_writing_a_file_triggers_the_stats_row` runs `setup_notifications()` on the bucket
  `wikipulse-test-worker`, starts the listener in a thread, writes an hour with
  `LakeStore.write_hour()` and waits for the row (6 edits, 1 page, 3 editors). Then it
  merges 4 more edits into the hour, as the archiver does, and the row follows (10
  edits, 2 pages). No polling of the lake anywhere: RustFS publishes, the listener reacts.
- The same with the real command (`run listen --bucket wikipulse-test-worker` in the
  background, a script writing the files): the row for a 20-edit hour appeared about a
  second after the write.

### Acceptance criterion 2: a deliberately failing handler ends up in the dead-letter list

- Unit: `test_a_deliberately_failing_handler_ends_up_in_the_dead_letter_list` (a handler
  that always raises: 3 attempts, waits 2 s and 4 s, one record with event, attempts and
  error).
- Integration: `test_a_failing_handler_ends_up_in_the_dead_letter_list` uploads a file
  named `part-0.parquet` that isn't Parquet. The notification arrives, the handler fails
  on every attempt, and the dead-letter record shows the event
  (`s3:ObjectCreated:CompleteMultipartUpload`), 2 attempts (the test's setting) and
  pyarrow's error.
- With the real command: three attempts (`retrying in 0.5 s`, `1.0 s`), then
  `run dead-letters` showed

  ```
  1 dead-lettered event(s) in wikipulse-test:demo:dead
  - 2026-11-09T17:00:54+00:00  wikipulse-test-worker/demo-5926/edits/year=2026/month=11/day=07/hour=14/part-0.parquet  (s3:ObjectCreated:CompleteMultipartUpload)
      3 attempts, last error: ArrowInvalid: Could not open Parquet input source ...: Parquet magic bytes not found in footer. ...
  ```

  After replacing the file with valid data, `run redrive` printed `1 succeeded, 0 failed
  again` and the hour's row appeared. The demo data was deleted afterwards.

### Requirement 3: backfill on the real lake

`uv run python -m wikipulse.workers.setup` configured `wikipulse-lake`, then
`uv run python -m wikipulse.workers.run backfill` handled the whole lake, none of which
had ever produced a notification: **77 hours (74 of history, 3 live), 77 missing, 77
handled, 0 failed, in 3.4 s.** The rows add up to 366,552 history edits and 4,481 live
edits, exactly the counts from 06a. Cross-check of one hour (7 November, 12:00, history)
against DuckDB over the same file: 5,087 edits, 3,914 pages, 994 editors, 37.68% bots in
both. The most edited page that hour was "Hiba Tawaji" (32 edits, 1 editor).
A second `backfill` finds nothing to do.

### Whole suite

`uv run pytest`: 821 passed. `uv run pytest -m integration`: 60 passed (all services).
`ruff check .` and `ruff format --check .` clean. The worker and lake unit tests also
pass on Linux (Docker, `ghcr.io/astral-sh/uv:python3.12-bookworm`).

### Look at it yourself (the spec's optional part)

The spec suggests explaining in your devlog how this would look on AWS. The table under
"The serverless pattern" above is a starting point: the bucket notification goes
straight to a Lambda function (no listener process: AWS runs the "subscribe and invoke"
part), Lambda retries twice, a failure goes to an SQS dead-letter queue, and an S3
Inventory report or a scheduled job does the backfill. To watch the local version:

```
uv run python -m wikipulse.workers.run                         # terminal 1
uv run python -m wikipulse.lake.archive --flush-interval 30    # terminal 2 (with the stream consumer running)
docker compose exec redis redis-cli SUBSCRIBE lake:notifications   # see the raw events
```

## Check your understanding

1. The worker was stopped for an hour while the archiver kept writing. What happens to
   the notifications of that hour, and how do the rows still come out right when the
   worker starts again?
2. Why does `NotificationListener.run()` subscribe *before* it runs the catch-up, and why
   is it fine that some hours are then handled twice?
3. The handler could have done `UPDATE hourly_stats SET edits = edits + <rows in the
   new file>`. Why would that give wrong numbers with this lake, even if every
   notification were delivered exactly once?

<details>
<summary>Answers</summary>

1. They are lost: RustFS publishes them on a Redis pub/sub channel, and pub/sub delivers
   only to clients subscribed at that moment. On start, the listener's catch-up
   (`backfill()`) lists the lake and compares each hour's files (size, last-modified
   time) with `source_size` and `source_modified` in `hourly_stats`; every hour that is
   missing or differs is handled again. The rows come from the files, not from the
   notifications, so nothing is missed.
2. A file written while the catch-up runs could otherwise fall into a gap: too late for
   the catch-up's listing and before the subscription existed. Subscribed first, its
   message waits in the subscription. Handling an hour twice is harmless because the
   handler recomputes the hour from all its files and upserts by (dataset, hour): the
   second write stores the same row.
3. The archiver (06a) rewrites the hour's single file with old *and* new rows on every
   flush, so "rows in the new file" includes everything already counted; the hour would
   be counted again on every flush. Recomputing the whole hour and replacing the row is
   correct no matter how often, or in which order, the hour is handled.

</details>

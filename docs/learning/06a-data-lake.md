# Learning note: Spec 06a, Data lake archive

## What was built

A second reader of the edit stream that keeps every kept edit forever, as hourly Parquet
files in S3-compatible object storage, plus a SQL command to query that history.

| File | Role |
|------|------|
| `docker-compose.yml` | New service `rustfs`: an S3-compatible object store (port 9000 for S3, 9001 for the web console). Volume `rustfs-data`. |
| `wikipulse/lake/partitions.py` | `hour_of(event_time)` (the UTC hour an edit belongs to), `partition_path()`, `part_path()`, `parse_partition()`: the `year=/month=/day=/hour=` layout. |
| `wikipulse/lake/table.py` | `EDIT_SCHEMA` (the archive's columns), `events_to_table()` (edits to an Arrow table), `merge_edits()` (concatenate, keep one row per `event_id`, sort). |
| `wikipulse/lake/store.py` | `s3_filesystem(settings)` (pyarrow's S3 client) and `LakeStore`: `read_hour`, `write_hour` (atomic replace), `merge_hour`, `hours`, `file_size`, `dataset()` (the whole archive for queries). |
| `wikipulse/lake/archiver.py` | `Archiver`: reads the stream with its own consumer group, buffers edits per event-time hour (`HourBuffer`), writes each hour with `merge_hour`, then `XACK`s that hour's entries. Re-reads its pending entries after a crash; retries when the lake is down. |
| `wikipulse/lake/archive.py` | `uv run python -m wikipulse.lake.archive`: the command line around the archiver. |
| `wikipulse/lake/query.py` | `uv run python -m wikipulse.lake.query "<SQL>"`: DuckDB over the archive, which appears as the table `edits`. |
| `wikipulse/config.py`, `.env.example` | `S3_ENDPOINT`, `S3_BUCKET`, `S3_REGION`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `LAKE_PREFIX`, `LAKE_GROUP`, `LAKE_FLUSH_INTERVAL_S`, `LAKE_MAX_BUFFER_ROWS`. |
| `scripts/lake_crash_test.py` | The scripted crash check: kills the archiver hard three times mid-hour, then compares the archive with the stream and with a crash-free run. |
| `scripts/lake_size_report.py` | One hour as Parquet vs. the same edits as JSON: sizes and DuckDB query time. Writes `evals/results/<date>_lake-size.md` and `.json`. |
| `scripts/fill_stream.py` | Bulk-loads a recording's kept edits into a stream of your own (same filter and normalization as stage 1), to archive recorded history quickly. |
| `tests/lake/` | 37 unit tests (local folder instead of S3, fakeredis instead of Redis) and 3 integration tests against the real RustFS and Redis. |
| `.github/workflows/ci.yml` | A `rustfs` service container and the `S3_*` variables, so CI runs the lake's integration tests too. |

How data flows:

```
wiki:edits (Redis Stream)
   ├── group detect:live  ─> spike detector (stage 2)          each group: own position,
   └── group lake:archive ─> Archiver                           own pending entries list
            │  XREADGROUP ... >        (after a crash first: ... 0 = my pending entries)
            ▼
        buffers[hour_of(event_time)]   one HourBuffer per hour: edits + their entry ids
            │  every LAKE_FLUSH_INTERVAL_S s, at LAKE_MAX_BUFFER_ROWS edits, and on Ctrl+C
            ▼
        LakeStore.merge_hour(hour)     read the hour's file, add the new rows, drop
            │                          duplicate event_ids, sort, upload as one object
            ▼
        XACK that hour's entry ids     only now

S3 bucket wikipulse-lake:
  edits/year=2026/month=11/day=07/hour=13/part-0.parquet
                                    ...
uv run python -m wikipulse.lake.query "SELECT ... FROM edits WHERE day = 7"
  -> LakeStore.dataset() (pyarrow, Hive partitioning) registered in DuckDB as `edits`
```

## Key concepts

### Object storage and the S3 interface

S3 (Amazon's Simple Storage Service) became the standard API for *object storage*: you put
a blob of bytes (an *object*) under a name (a *key*) in a *bucket*, and get it back with the
same name, over HTTP. There are no real folders. `edits/year=2026/month=11/day=07/hour=13/
part-0.parquet` is a single key; the "folders" are just common *prefixes*, and listing a
prefix (`LakeStore.hours()` lists `wikipulse-lake/edits/` recursively) is how you discover
what exists.

Two properties of S3 carry the whole crash-safety design:

- **An upload is all or nothing.** The new object becomes visible only once the upload
  completes. pyarrow even uses a *multipart upload* (`CreateMultipartUpload` ... `Complete`),
  and the object appears only on the final `Complete`.
- **Uploading to an existing key replaces the old object in one step.** Readers see the old
  file or the new one, never half of each and never nothing.

So `LakeStore.write_hour()` simply uploads the whole hour to `part-0.parquet`; that *is* the
atomic replace the spec asks for. (On a local folder, used by unit tests, the same effect
comes from writing `.tmp-<random>.parquet` and renaming it over the old file.)

The code never talks to RustFS specifically. `s3_filesystem()` builds pyarrow's generic S3
client from `S3_ENDPOINT` and the credentials. To use AWS instead, set
`S3_ENDPOINT=https://s3.eu-central-1.amazonaws.com`, `S3_REGION=eu-central-1` and real keys.
That is the "one address" the spec mentions.

Try it: open http://localhost:9001 (user `wikipulse`, password `wikipulse-lake`) and browse
the bucket `wikipulse-lake`.

### Columnar storage: why Parquet is smaller and faster

JSON stores *rows*: every edit repeats every field name (`"is_bot":false,"is_minor":...`)
and writes numbers and timestamps as text. Parquet stores *columns*: all 8,492 `event_time`
values of an hour together, then all titles, then all `is_bot` flags, and so on. Values of
one column look alike, so they compress very well:

- booleans become bits; timestamps that grow slowly become small differences
  (`event_time` is sorted because `merge_edits()` sorts each hour);
- repeated strings (`type` is only `edit` or `new`; bots make many edits) are stored once
  in a *dictionary* and referenced by number;
- a general compressor (zstd here, `LakeStore(compression="zstd")`) runs on top.

Reading is faster for the same reason. A query that needs `title` and `is_bot` reads only
those two columns from the file and skips the comments and user names; JSON must be parsed
completely, line by line. Parquet also stores the type of each column and min/max values
per block of rows, so an engine can skip blocks that can't match a filter.

The measured difference on one real hour is in "Results" below: Parquet is about **6x
smaller** than the JSON the stream holds, smaller even than gzipped JSON, and the same
DuckDB query is about **15x faster**.

### Partitioning: `year=/month=/day=/hour=`

`partition_path()` turns the hour of an edit's **event time** into folders named
`name=value`. That naming is the *Hive* convention (from Apache Hive, the first SQL engine
on Hadoop), and every query engine understands it: `LakeStore.dataset()` tells pyarrow
`partitioning=ds.partitioning(PARTITION_SCHEMA, flavor="hive")`, so the folder names become
the columns `year`, `month`, `day` and `hour`, although no file stores them.

Why engines love it: a filter on a partition column is answered from the *key names*
alone. `WHERE day = 6 AND hour = 14` opens exactly one file of the 74; the other 73 are
never downloaded. On the archived history, counting all 366,552 edits took 0.16 s; the
same count for one hour took 0.02 s. With years of data the difference is what makes a
lake usable.

Choosing the partition is a trade-off. Too coarse (one file per month) and every query
reads too much; too fine (one file per minute) and you get the *small files problem*:
thousands of tiny objects, each with its own request and metadata overhead. An hour of
English Wikipedia edits is 0.2 to 0.6 MB of Parquet, which is already on the small side
(engines like files of tens to hundreds of MB), so splitting an hour into several parts
would only make it worse. That is why every hour is a single `part-0.parquet`.

Late events follow from the same function: `hour_of(event.event_time)` uses the edit's own
timestamp, never the clock. An edit from 11:58 that arrives at 12:03 is buffered for 11:00;
the next flush merges it into the 11:00 file that may already exist
(`test_late_edit_joins_its_already_archived_hour`).

### A second consumer group

Stage 2's detector reads `wiki:edits` with the consumer group `detect:live`. The archiver
creates its own group, `lake:archive` (`Archiver.ensure_group()`). Each group has

- its own position (`last-delivered-id`): which entries it has been given, and
- its own *pending entries list* (PEL): entries given out but not yet acknowledged.

So the two readers are independent. The archiver can be stopped for an hour and catch up
later; the detector neither waits for it nor loses entries to it
(`test_another_consumer_group_reads_independently`). After this spec's run you can see
three groups on the live stream:

```
docker compose exec redis redis-cli XINFO GROUPS wiki:edits
```

The archiver's crash safety is the "acknowledge after the work is durable" pattern from
stage 2, applied to files instead of detector state:

1. `XREADGROUP ... >` hands out new entries; they enter the PEL.
2. `_add()` puts each edit into the buffer of its hour, remembering the entry id.
3. `flush()` writes an hour with `merge_hour()` and only then calls `_ack()` for that
   hour's entry ids.

A crash at any point is safe:

- before the write: the entries are still pending; on restart `_read_pending()` reads
  them again with `XREADGROUP ... 0` ("my pending entries") and writes them;
- after the write, before `XACK`: they are read and merged again, but `merge_edits()`
  keeps one row per `event_id`, so the file comes out identical;
- during the upload: S3 keeps the old object.

This is *at-least-once delivery plus an idempotent write*, the same recipe as stage 1's
Lua script, here giving "exactly once" rows in the lake.

One limit to know: the stream is trimmed at about `STREAM_MAXLEN` (1,000,000) entries. An
archiver stopped for so long that the stream trims entries it hasn't read loses them; with
English Wikipedia's ~5,000 kept edits per hour that would take about a week.

## Design decisions and deviations from the spec

- **RustFS instead of MinIO.** The spec allows an alternative "if MinIO's free edition
  doesn't suit". It doesn't any more: MinIO stopped publishing community images, and on
  this machine `docker pull minio/minio` answers "repository does not exist" and
  `quay.io/minio/minio` answers 401. RustFS (`rustfs/rustfs:1.0.4`, Apache 2.0, a stable
  1.x release) is a MinIO-compatible S3 server configured entirely by environment
  variables (`RUSTFS_ACCESS_KEY`, `RUSTFS_SECRET_KEY`), with a web console. SeaweedFS
  (also pulled and considered) needs a start command; RustFS's image runs as-is, which
  also lets CI use it as a plain service container. The Compose service is called
  `rustfs`; the settings are generic `S3_*` names, because the code only speaks S3. The
  image is pinned, so an upstream change can't silently break the lake.
- **pyarrow for both Parquet and S3, no boto3.** pyarrow (needed for Parquet anyway) has
  its own S3 filesystem (`pyarrow.fs.S3FileSystem`), so one library reads, writes, lists
  and queries. New dependencies: `pyarrow` and `duckdb` (both named or implied by the spec).
- **DuckDB reads the lake through a pyarrow dataset**, not through DuckDB's own S3 support
  (the `httpfs` extension). `httpfs` is downloaded from the internet the first time it is
  used and needs its own copy of the credentials; registering `LakeStore.dataset()` in
  DuckDB (`query.connect()`) reuses the same S3 client and settings, works offline, and
  still gets column selection and partition pruning (DuckDB passes its filters to the
  pyarrow scanner). The SQL is the same either way.
- **One file per hour, rewritten whole (merge) instead of appending part files.** The spec
  names files `part-<n>.parquet` and asks for "write by hour, replace atomically". Several
  parts per hour can't be replaced together atomically (S3 has no multi-object
  transaction), so each hour is exactly one object, `part-0.parquet`, rewritten with old
  plus new rows on every flush. The `<n>` stays in the name (`part_path(..., n=0)`) so the
  layout matches the spec and a future writer could split huge hours. A rewrite costs
  about 0.3 s per hour file, so the default flush every 5 minutes rewrites each hour about
  12 times; that is cheap at this size.
- **When to write.** Not when an hour "closes" (that needs a guess of how late events can
  be), but every `LAKE_FLUSH_INTERVAL_S` (300 s, wall clock), whenever `LAKE_MAX_BUFFER_ROWS`
  (50,000) edits are buffered, and at shutdown. Late events need no special case: they
  are merged into their hour whenever they come. The wall clock only decides *when* to
  write, never *what*: `merge_edits()` sorts each hour by `(event_time, event_id)`, so the
  file content doesn't depend on flush timing. The crash test checks exactly that: the
  crashed-and-restarted archive holds the same tables, row for row, as a crash-free run.
- **Duplicates are defined by `event_id`.** Stage 1 deduplicates within an hour of
  arrival; replays, overlapping recordings and re-deliveries after a crash can still
  repeat an edit. `merge_edits()` keeps the first copy (the one already archived).
- **Acknowledge per hour.** Each hour's entries are acknowledged right after that hour's
  file is written, so if a later hour fails, the earlier ones are not re-done.
- **The lake being down is not fatal.** A failed write (pyarrow raises `OSError`) is
  logged; the edits stay buffered and pending, and `_maybe_flush()` retries after 5 s,
  doubling to at most 300 s (`test_lake_outage_is_retried_without_losing_edits`). The final
  flush at shutdown does raise, which leaves the entries pending for the next start.
- **Pending entries are re-read once per process** (`_recovered`), so a long-running
  archiver never buffers the same entry twice.
- **Columns.** All `EditEvent` fields, with `event_time` and `received_at` as UTC
  timestamps. Partition columns are not stored in the files. `received_at` is wall-clock
  metadata, so it differs between a live run and a replay; everything else is identical.
- **`query` shows times in UTC** (`SET TimeZone = 'UTC'`): DuckDB otherwise shows
  timestamps in the computer's local time zone, which made 12:00 UTC look like 14:00 next
  to `hour = 12`.
- **`scripts/fill_stream.py` instead of `replay` for big history.** `wikipulse.stream.replay`
  managed only about 450 messages per second here (one Redis round trip per message,
  through the crash-safe live code path), so the 12.5-hour recording alone would have
  taken over an hour. The bulk loader applies the same `is_wanted` and `normalize`, writes
  the same entry fields in pipelined batches, and loaded all 66 hours of recordings in
  about 4 minutes. So instead of scaling the archive run down, the whole recorded history
  was archived.
- **Where the data went.** The live stream `wiki:edits` (4,481 edits from 7 November) was
  archived with the defaults into `wikipulse-lake/edits/`, so the consumer group
  `lake:archive` now exists on the live stream and the next `archive` run continues from
  there. The recorded history went into a separate prefix, `wikipulse-lake/history/edits/`,
  so you decide whether recordings belong in the main archive (`--prefix history/edits` to
  query it). The temporary stream `wiki:lake:history:edits` (376k entries, 180 MB of Redis
  memory) was deleted after archiving.
- **Side effect on replay keys.** A first attempt to load history with
  `wikipulse.stream.replay` (stopped after about a minute because it was too slow) wrote
  to its own stream (since deleted) but, like every replay, reset `wiki:replay:stats` and
  moved `wiki:replay:last_id`. Your earlier replay statistics in those two keys are gone.
- **CI** gets a `rustfs` service container (same image, dev credentials) and the `S3_*`
  variables; the lake's integration tests do not skip, matching the other integration
  tests that also expect their services. Not validated with actionlint; it runs on the
  next push.
- **`wikipulse.check` doesn't check S3 yet** (left out to keep the change to what the spec
  lists). If the object store is down, `archive` and `query` fail at start with pyarrow's
  connection error after about 15 s of retries.

## Results

All measured on 2026-11-09 on the human's Windows machine, with RustFS and Redis in Docker.

### Acceptance criterion 1: unit tests for buffering and partition paths; integration tests

- `tests/lake/test_partitions.py` (8 tests): hour flooring, time zones, naive times
  rejected, zero-padded Hive paths, parsing keys back into hours.
- `tests/lake/test_archiver.py` (12 tests): buffering by event-time hour, nothing
  acknowledged before its hour is written, a failed write leaves entries pending and
  buffered, lake outage retried, crash mid-hour, crash between write and `XACK`, late
  edits, re-running the whole stream changes nothing, malformed entries, buffer-size and
  interval flushes, independence from another consumer group.
- `tests/lake/test_table_and_store.py` (13) and `tests/lake/test_query.py` (4): schema,
  merge without duplicates and independent of arrival order, atomic replace leaves no temp
  files, partition columns in the dataset, SQL over the archive.
- `tests/lake/test_lake_s3.py` (3, `-m integration`): round trip and replace on the real
  RustFS (only one object per hour afterwards), the archiver crashing mid-hour on the real
  Redis and RustFS, and the `archive` and `query` commands end to end. Each test uses its
  own random prefix in the bucket `wikipulse-test` and its own stream, and deletes both.
- Whole suite: `uv run pytest` 764 passed; `uv run pytest -m integration` 56 passed;
  `ruff check .` and `ruff format --check .` clean.

### Acceptance criterion 2: after a crash mid-hour, no gaps or duplicates (scripted)

`uv run python scripts/lake_crash_test.py --recording data/recordings/history-20261104T1200Z.jsonl`
(the first 1,000,000 recording lines):

- Stream: 26,441 edits over 7 hours (04 Nov 12:00 to 18:00 UTC), including 5 late edits
  appended at the end with event times in the first hour.
- The archiver ran as a child process writing every 1,500 edits and was killed hard
  (`TerminateProcess`) three times:
  - at 7,000 entries read: 1,000 edits unacknowledged, 905 of them in the 13:00 hour,
    which already had a file;
  - at 14,000: 2,000 unacknowledged, all in 15:00, which already had a file;
  - at 20,000: 2,000 unacknowledged, 1,812 in 16:00, which already had a file.
  So every kill hit the middle of a partly archived hour.
- After the final restart: **26,441 rows; 0 missing, 0 extra, 0 duplicates, 0 in the wrong
  hour, all 5 late edits archived, 0 entries still pending, and every hour identical to the
  crash-free reference run. PASS.** (An earlier run on 400,000 lines, 2 hours, also passed.)

### Acceptance criterion 3: one hour as Parquet vs. the same edits as JSON

`uv run python scripts/lake_size_report.py --prefix history/edits` picked the largest hour,
5 November 01:00 UTC, with 8,492 edits (`evals/results/2026-11-09_lake-size.md`):

| Format | Size | vs. JSON |
|---|---:|---:|
| JSON Lines, as in the Redis Stream | 3,406 KiB | 100% |
| JSON Lines, gzip | 664 KiB | 19.5% |
| **Parquet, zstd (what the lake stores)** | **556 KiB** | **16.3%** |
| Parquet, snappy | 844 KiB | 24.8% |
| Parquet, uncompressed | 1,142 KiB | 33.5% |

Even uncompressed, Parquet is a third of the JSON: that part is the columnar layout and
encodings alone (no repeated field names, bits for booleans, dictionaries for repeated
strings). The same DuckDB query (top 10 titles with bot share) took a median **80 ms
over the JSON file and 5 to 6 ms over the Parquet file**, about 15x faster. That is about
410 bytes per edit as JSON against 67 as Parquet.

### The archive runs

- History: all four history recordings (66 hours, 4 to 7 November) bulk-loaded into a
  stream of 375,750 entries and archived in two runs (60,612 entries in 15 s, then
  315,138 in 46 s, including start-up). The lake holds **366,552 edits in 74 hourly files,
  26.3 MB**; the 9,198 entries fewer are edits that appeared in two overlapping recordings
  and were merged away. A SQL check found 0 rows whose event time is outside their
  partition's hour, and `count(*) = count(DISTINCT event_id)`.
- Live: `wiki:edits`, 4,481 edits into 3 hourly files (0.3 MB).

### Look at it yourself (the spec's optional part)

```
uv run python -m wikipulse.lake.query --prefix history/edits "SELECT day, count(*) AS edits, round(100*avg(is_bot::int),1) AS bot_pct FROM edits GROUP BY day ORDER BY day"
```

| Day (Nov.) | Edits | Hours | Bot share |
|---|---:|---:|---:|
| 4 | 56,768 | 12 | 15.2% |
| 5 | 109,579 | 24 | 22.0% |
| 6 | 127,199 | 24 | 34.2% |
| 7 | 73,006 | 14 | 42.6% |

Edits per hour ranged from about 2,400 to 8,500. The bot share is far from constant: a
few busy bot runs move it a lot, which is worth remembering when stage 2's detector
ignores bots. Most edited articles by humans on 6 November: "Frozen yogurt" (142 edits by
81 editors), "2026 United States elections" (92 by 50), "2026 United States House of
Representatives elections" (81 by 39). Note "Human impact on the environment" (77 edits by
2 editors): many edits by few people is ordinary work, not news, which is why the detector
also counts editors.

## Check your understanding

1. The archiver has written the 14:00 file and is killed before `XACK`. On restart it reads
   those entries again and writes 14:00 again. Why doesn't the hour end up with every edit
   twice, and what would go wrong if each flush wrote a new `part-<n>` file instead?
2. An edit from 13:59:58 reaches the stream at 14:00:03, after the 13:00 file was written.
   Where does it end up, and which function decides that?
3. Why is `SELECT count(*) FROM edits WHERE day = 6 AND hour = 14` much faster than the
   same count without the filter, even though the files don't contain `day` or `hour`?

<details>
<summary>Answers</summary>

1. Every write is a merge: `merge_hour()` reads the existing 14:00 file, adds the buffered
   rows, and `merge_edits()` keeps one row per `event_id` before replacing the object. The
   re-delivered edits are already in the file, so the result is the same file. With a new
   part file per flush, the second write would be a second object holding the same edits
   (duplicates), unless part names were derived from the content, and any crash between
   writing several parts and deleting old ones would leave duplicates too. One object per
   hour, replaced atomically, avoids all of that.
2. In the 13:00 hour: `hour_of(event.event_time)` uses the edit's own timestamp. The next
   flush merges it into the existing 13:00 file (`test_late_edit_joins_its_already_archived_hour`).
   Using the arrival time would have put it into 14:00, and a replay could put it somewhere
   else again.
3. `day` and `hour` come from the key names (`.../day=06/hour=14/...`, Hive partitioning).
   The filter is applied to the list of keys before any file is opened, so only one of 74
   objects is downloaded and read (partition pruning).

</details>

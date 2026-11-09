# Review: 06a-data-lake

Reviewer: independent agent. Branch `spec-06a-data-lake` (1 commit, `ae1b5bf`) against `main`.
All checks were run on 2026-11-09 against the running Docker services (Redis, RustFS 1.0.4 on port 9000).

## Acceptance criteria

| Criterion | Result | Evidence |
|---|---|---|
| Unit tests for buffering and partition paths | pass | `uv run pytest`: 764 passed, 60 deselected. `tests/lake/test_partitions.py` (8), `test_archiver.py` (12), `test_table_and_store.py` (13), `test_query.py` (4). They cover event-hour buffering, no ack before write, failed write keeps entries pending, crash between write and XACK (via monkeypatched `_ack`), late edits, idempotent re-run, and an independent second group. |
| Integration tests against MinIO (RustFS) | pass | `uv run pytest -m integration tests/lake`: 3 passed (round trip and replace on RustFS, crash mid-hour on real Redis and RustFS, `archive` and `query` CLIs end to end). |
| After a crash mid-hour, no gaps or duplicates (scripted) | pass | `scripts/lake_crash_test.py --recording data/recordings/history-20261104T1200Z.jsonl`: 26,441 edits over 7 hours, killed at 7,000, 14,000 and 20,000 entries read. Each kill left unacknowledged edits in an hour that already had a file. Final result: "Missing 0, extra 0, duplicates 0, in the wrong hour 0, late edits archived True, still pending 0, identical to the reference run True ... PASS". It took about 20 s. |
| Size of one hour as Parquet vs. JSON | pass | Re-ran `lake_size_report.measure()` with the work dir moved to the scratchpad so the committed `evals/results` file stays unchanged. The hour 2026-11-05T01 (8,492 edits) gave JSON 3,406 KiB, JSON gzip 664 KiB, Parquet zstd 556 KiB (16.3%), snappy 844 KiB, uncompressed 1,142 KiB. These match the committed `evals/results/2026-11-09_lake-size.md`. Query time was JSON 72 ms vs. Parquet 6 ms. |
| Req. 1: ack only after the file is written | pass | `Archiver.flush()` calls `merge_hour` then `_ack` for each hour. Covered by the unit tests and the crash test (0 pending, 0 missing). |
| Req. 2: re-run never duplicates rows | pass | `merge_edits` keeps one row per `event_id` with a deterministic sort. `test_rerunning_the_whole_stream_changes_nothing` passes. The DuckDB check on `history/edits` gave 366,552 rows and 366,552 distinct `event_id`. On `edits` (live) it gave 4,481 rows and 4,481 distinct. |
| Req. 3: late events go to their event-time hour | pass | The crash test's 5 late edits were archived in the first hour. Across all 366,552 history rows, DuckDB found 0 where `date_trunc('hour', event_time)` differs from the folder's year/month/day/hour. |
| Outputs: compose service, `archiver.py`, `archive` and `query` CLIs, `S3_ENDPOINT` / `S3_BUCKET` / credentials config | pass | All present. `.env.example` is updated with local dev defaults only, and no real email or secret is in the diff. |
| Optional: query a day with DuckDB | pass | Edits per hour and bot share for 5 November (24 rows), the top pages, and an invalid SQL (exit 1 with a clean error) all worked. |
| Definition of done: ruff, learning note | pass | `ruff check .` clean, `ruff format --check .` reported 321 files already formatted, and `docs/learning/06a-data-lake.md` has all sections and three questions in `<details>`. |

**RustFS instead of MinIO:** acceptable. The spec allows an S3-compatible alternative if the choice is explained, and it is (compose comment and learning note). The image is pinned (`rustfs/rustfs:1.0.4`), the code only knows `S3_ENDPOINT` through pyarrow's generic `S3FileSystem`, and the integration test checks the one property the design relies on: an overwritten key leaves exactly one object. RustFS is younger than MinIO, so this pinned version should only be upgraded deliberately.

## Problems

1. **medium**, `wikipulse/lake/store.py:110-119` (with `wikipulse/lake/archive.py:35`). `merge_hour` reads, merges and rewrites an hour's file without any lock or single-writer guard. If two archivers run at once (a second accidental start, or `--consumer archiver-2` on the same group), they can overwrite each other's file for the same hour, and both acknowledge their entries, so rows are silently lost.
2. **medium**, `wikipulse/lake/archiver.py:152-166`. Crash recovery re-reads only the current consumer's pending entries (`XREADGROUP ... 0`), with no `XAUTOCLAIM`/`XCLAIM`. If the archiver restarts under a different `--consumer` name, entries left pending by the old name are never archived, which leaves a permanent gap with no warning.
3. **low**, `wikipulse/lake/archive.py:79-80`. `args.flush_interval or settings.lake_flush_interval_s` treats `--flush-interval 0` as "not given", and the CLI values skip the `gt=0` / `ge=1` checks that the config fields have (for example, a negative `--max-buffer-rows` is accepted).
4. **low**, `scripts/lake_crash_test.py:122-133`. The kill fires as soon as `entries-read` crosses the target, which always happens right after an `XREADGROUP` batch (the kills land exactly on 7,000/14,000/20,000, the same as in the learning note). So the scripted test never kills during an upload or between write and `XACK`; only the monkeypatched unit test covers that gap.
5. **low**, `wikipulse/lake/store.py:74-82` and `wikipulse/lake/query.py:333`. `LakeStore.from_settings` creates the bucket if it is missing, even for a read-only `query`. A typo in `S3_BUCKET` therefore creates an empty bucket and returns 0 rows instead of failing.
6. **low**, `wikipulse/lake/store.py:97-103`. The atomic-replace claim for S3 assumes `pq.write_table` never finalizes a partial object. In pyarrow 26, `write_table` uses `ParquetWriter` as a context manager, whose `__exit__` closes (and so completes) the output stream even when the write raised. I did not verify that a failed mid-write upload is aborted rather than committed. A hard kill is safe either way.
7. **low**, `wikipulse/lake/query.py:344`. An error is reported with `print(..., file=sys.stderr)` inside the package, against the AGENTS.md rule to log with `logging`. Printing the result table itself is fine.
8. **low**, `wikipulse/lake/archiver.py:43-45, 76-77` and `wikipulse/lake/store.py:44-45`. The retry backoff, S3 timeouts and the `Archiver` constructor defaults (`300.0`, `50_000`, which duplicate the config defaults) are hard-coded module values. This is consistent with `detect/runner.py`, but the duplicated defaults can drift from `config.py`.
9. **low**, `scripts/fill_stream.py:246-263`. The docstring says it applies the same deduplication as the stage 1 consumer. In fact it deduplicates across the whole recording with an unbounded `seen` set, while stage 1 deduplicates within a one-hour window.
10. **low**, `scripts/lake_crash_test.py:50` and `scripts/lake_size_report.py:39-40`. `Path("data")` and `Path("evals")` are relative to the working directory, so running the scripts from anywhere other than the repo root writes their output to the wrong place.
11. **low**, `tests/lake/test_lake_s3.py:86`. `test_hour_round_trip_and_atomic_replace` checks that a write replaces the old file and leaves one object, but not atomicity (no reader runs during the write), so the name claims more than the test proves.
12. **low**, `docs/learning/06a-data-lake.md` (design decisions, "Side effect on replay keys"). While building this spec, the implementer reset the human's `wiki:replay:stats` and moved `wiki:replay:last_id`, which are outside the spec's scope. It is disclosed, but that state is gone.

Verdict: fix

Problems 1 and 2 are small to fix and affect the component's core promise (no gaps, no lost rows): a single-writer lock (for example a Redis `SET NX` key with a TTL, renewed while running) plus either removing `--consumer` or claiming other consumers' pending entries on start. Everything the spec asks for works as specified.

## Questions for the human before merging

1. The archiver writes an hour's file and only then sends `XACK`. Walk through what happens if the process is killed right after the upload but before `XACK`. Which function makes the restart produce the same file rather than doubled rows, and why does it keep the first copy of a duplicate?
2. Every flush reads the existing `part-0.parquet` for an hour, merges in new rows and rewrites the whole object. What would go wrong if two archiver processes did this for the same hour at the same time, and why can't the consumer group alone prevent it?
3. Why is each hour a single object rewritten whole, instead of new `part-1`, `part-2`, ... files appended per flush? What does that choice cost when a late edit arrives for an hour that was archived days ago?

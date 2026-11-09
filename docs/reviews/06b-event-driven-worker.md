# Review: Spec 06b, Event-driven worker

Reviewer: independent agent. Branch `spec-06b-event-driven-worker` (commit `eee46e9`), diffed against `main`.
Date: 2026-11-09.

## Acceptance criteria

| Criterion | Result | Evidence |
|---|---|---|
| Unit tests for the handler | pass | `uv run pytest tests/workers`: 42 passed (handler numbers, empty hour, row-order independence, key parsing, double delivery, rewritten hour, multi-part hours, skipped objects, corrupt file, newer-row protection). Full suite: 821 passed, 64 deselected. |
| Integration test: writing a file triggers the stats row | pass | `uv run pytest -m integration tests/workers`: 4 passed in 19 s. `test_writing_a_file_triggers_the_stats_row` writes through `LakeStore`, RustFS publishes, the threaded listener writes the row, and a merge makes the row follow (6 then 10 edits). Also by hand: `workers.setup --bucket wikipulse-review-06b`, then `workers.run listen` in the background; writing an hour gave the row "5 edits, 2 pages, 3 editors, 20% bots" about 1 s after the write. |
| A deliberately failing handler ends up in the dead-letter list | pass | Unit `test_a_deliberately_failing_handler_ends_up_in_the_dead_letter_list` and integration `test_a_failing_handler_ends_up_in_the_dead_letter_list` both pass. By hand: a non-Parquet `part-0.parquet` produced attempts 1 and 2 (0.2 s, 0.4 s), then "added to dead-letter list review06b:dead". `run dead-letters` showed the record (`s3:ObjectCreated:CompleteMultipartUpload`, 3 attempts, ArrowInvalid). `run redrive` while the file was still broken: "0 succeeded, 1 failed again". After I replaced the file: "1 succeeded, 0 failed", list empty, row present (2 edits). |
| Req. 1: idempotent (upsert by hour) | pass | PK `(dataset, hour)`, `ON CONFLICT DO UPDATE`. The unit and integration idempotence tests pass. The double-delivered notification in `test_listener.py` gives one row. |
| Req. 2: retries, then dead-letter | pass | See above. Delays double (2 s, 4 s) per `test_a_failure_is_retried_with_growing_delays`. |
| Req. 3: backfill of files without notification | pass | Integration `test_backfill_handles_files_that_never_got_a_notification` passes. A read-only comparison of `lake_hours()` with `stored_sources()` on the real lake gives 77 hours, 0 missing, 0 stale, so the tz-aware mtime and size comparison round-trips through Postgres correctly. |
| hourly_stats is correct on real data | pass | All 77 real rows (74 `history/edits`, 3 `edits`) checked against DuckDB `read_parquet` on the same folders (edits, distinct titles, distinct users, bot edits): 77 rows, 0 mismatches. Top-3 pages also matched DuckDB for 3 sampled hours. |
| ruff | pass | `ruff check .`: all checks passed. `ruff format --check .`: 344 files already formatted. |
| No secrets or real email in committed files | pass | Grepped the diff. The only key-like string is AWS's public documentation example key in the SigV4 test. |

I deleted all of my own review data afterwards (bucket `wikipulse-review-06b`, its 2 rows, Redis key `review06b:dead`). `hourly_stats` still has its 77 real rows. No processes are left running.

## Problems

1. **medium**, `wikipulse/workers/listener.py:248-257`: the listener catches only `redis.ConnectionError`. If the catch-up backfill fails with a Postgres error (`stored_sources`) or an S3 error (`lake_hours`), the exception escapes `run()` and kills the long-running worker. This contradicts the reconnect-and-catch-up resilience that the module and `PostgresStats` promise. It happens at startup, and also after any Redis reconnect when another service is briefly down.
2. **low**, `wikipulse/workers/backfill.py:272-284`: an hour whose file is permanently broken has no row, so every catch-up (each `listen` start, each Redis reconnect, each manual `backfill`) retries it, with the full backoff sleeps, and pushes one more dead-letter record. I reproduced this: two `backfill` runs gave 2 identical letters for one hour. The list grows without bound and `redrive` then repeats the work.
3. **low**, `wikipulse/workers/invoker.py:162-166`: `redrive` takes a record off the list with `RPOP` before retrying it, so a crash or kill during the retry loses the record. `LMOVE` to a processing list would avoid this. The backfill covers missing hours, but not hours that only went stale.
4. **low**, `wikipulse/db/hourly_stats.py:35`: the `source_modified <=` guard assumes the newest mtime of an hour never goes down. If a newer part file is ever deleted (the layout allows `part-<n>`), the upsert is refused for good, and the backfill reports the hour as stale on every run without being able to fix it.
5. **low**, `wikipulse/db/hourly_stats.py:18`: the `db` layer imports `HourStats` from `wikipulse.workers`, which reverses the usual dependency direction (the other `db` modules don't depend on their callers). Not a bug.
6. **low**, `wikipulse/workers/listener.py:29`, `wikipulse/workers/s3_api.py:104`, `wikipulse/workers/setup.py:65`: these tunables are hard-coded instead of living in `config.py`: the reconnect cap (30 s), the HTTP timeout (15 s), and the target-online polling (10 tries x 1 s). Earlier specs keep similar constants as module constants, so this is minor.
7. **low**, `docs/learning/06b-event-driven-worker.md:225`: the note says "Not committed ... all changes are in the working tree", which is no longer true now that the work is committed on the branch.

Verdict: merge

(No high problems. Problem 1 is worth a small follow-up: catch exceptions around `catch_up()` and retry it with the same backoff.)

## Questions for the human before merging

1. The worker subscribes to the Redis channel *before* it runs the catch-up backfill. What could be lost if the order were reversed? And why is it harmless that a file written during the catch-up might be handled twice?
2. The upsert has `WHERE hourly_stats.source_modified <= EXCLUDED.source_modified`. What situation does this guard protect against? And why is the handler still idempotent without the guard when only one worker runs?
3. On AWS, which component would replace each of these local pieces: RustFS notification target, Redis pub/sub channel, `NotificationListener` plus `Invoker`, and the Redis dead-letter list? Which of them has no AWS counterpart, because AWS's delivery is at-least-once rather than fire-and-forget?

# Review: Spec 02a, Spike detector

Branch `spec-02a-spike-detector`, one commit (`ad9f379`), 30 files, +2943/-3 against `main`.
Reviewed 2026-11-07 against `docs/specs/02a-spike-detector.md` and `AGENTS.md`.

## Acceptance criteria

| Criterion | Result | Evidence |
|-----------|--------|----------|
| Replaying the same recording twice produces identical spikes | pass | `scripts/determinism_check.py --file data/recordings/crashtest-20261107T012834Z.jsonl` (with `DETECT_MIN_EDITS=2 DETECT_MIN_EDITORS=2 DETECT_LONG_MIN_EDITS=3 DETECT_LONG_MIN_EDITORS=2` so the 6-minute recording yields spikes): 525 entries in each replay, run B stopped after 262 entries and resumed for 263, `Spikes: A 3, B 3`, `PASS: both replays produced identical spikes (all fields compared)`. I used a smaller recording than the learning note's 1-hour, 235 MB one to keep the run short. Also covered by the unit tests `test_same_input_gives_the_same_spikes`, `test_snapshot_and_restore_half_way_gives_the_same_spikes` and `test_crash_and_restart_give_the_same_spikes`, plus the integration tests `test_detect_run_end_to_end_on_real_services` and `test_crash_between_snapshots_loses_and_doubles_nothing`. |
| Unit tests for window counting, baseline and lifecycle logic | pass | `tests/detect/test_windows.py` (10 tests), `test_baseline.py` (12), `test_detector.py` (lifecycle, cold start, bots, reverts, hysteresis, ending, late events, determinism, using the fixture through `fixture_events()`), `test_runner.py` (fakeredis consumer group, crash and restart). `uv run pytest`: 132 passed, 12 deselected. `uv run pytest -m integration`: 12 passed. The tests check real behaviour with exact values (started_at, first_edit_at, counts, score), not just "no exception". |
| `trending` shows sensible pages during a live run | pass (from the stored live run) | I didn't start a new live session, as instructed. `uv run python -m wikipulse.detect.trending` against the run `live` that is already in Postgres prints 3 active spikes: *Dixons Brooklands Academy*, *Ramayana: Part 1*, *Results of the 2026 New Zealand general election*. `--run-id live-loose --all` lists 17, including *2026 New Zealand general election* and *2026 Men's Rugby League World Cup final*. Plausible, with some noise, as the learning note says. |
| Also build: migrations + `schema_migrations` + `python -m wikipulse.db.migrate` | pass | `uv run python -m wikipulse.db.migrate` prints "Database is up to date; nothing to apply." `test_migrations_apply_once_in_a_fresh_schema` applies the migrations in a fresh schema and checks that a second run applies nothing. |
| Also build: `wikipulse/db/spikes.py` with integration tests | pass | `tests/db/test_spikes_db.py`: upsert, idempotency, queries, run separation, end-to-end CLI run. All pass on the Docker Postgres. |
| Also build: detect tests using replays of `tests/fixtures/stream_sample.jsonl` | pass | `tests/detect/test_runner.py::replay_fixture` fills the stream through the stage-1 `Consumer` from the fixture. `helpers.fixture_events()` is used in the detector tests. |
| Requirements 1-5 (bots excluded, lifecycle, recorded fields, restart-safe state, determinism) | pass, with problem 1 | Bots only advance the watermark (`test_bot_edits_are_excluded`). Lifecycle with quiet period and hysteresis is tested. State is snapshotted to Redis atomically together with `XACK`. All spike fields are recorded, but `peak_edits_10m` can be wrong (problem 1). |
| Definition of done: ruff | pass | `uv run ruff check .`: "All checks passed!". `uv run ruff format --check .`: "94 files already formatted". |
| Rules: no data/secrets/real email committed, no new dependencies | pass | `git ls-files data` is empty. No email address appears in the diff. `pyproject.toml` and `uv.lock` are unchanged. |

## Problems

1. **medium**, `wikipulse/detect/detector.py:283`: when a spike starts on the long-window rule, `peak_edits_10m` is set to the *current* short-window count, so a denser 10-minute stretch that is already counted in `total_edits` (and lies after `first_edit_at`) is ignored. Reproduced: 4 edits in 2 minutes, then 6 more spread out, gives `total_edits=10, peak_edits_10m=2`. Live example: *2026 Men's Rugby League World Cup final* in `live-loose` shows peak 1 with 6 edits. This violates requirement 3's "peak edits per 10 minutes".
2. **low**, `wikipulse/detect/detector.py:225` and line 284: the score is the maximum of a short-window z and a long-window z. Because the floor scales with the window, long-rule spikes systematically get low scores (for example 1.7 for the Rugby final). `trending` and `active_spikes` sort by score, so these spikes always rank below short-rule spikes. The learning note doesn't mention that the two scores aren't comparable.
3. **low**, `wikipulse/detect/trending.py:35` and line 83: the "detector time" in the header, and the duration of active spikes, use the newest `last_edit_at` of any spike in the run, not the detector's watermark. When no spike has had a recent edit, the header shows a stale time and the durations are too short.
4. **low**, `wikipulse/detect/detector.py:51-56` and `wikipulse/detect/runner.py:91`: `SWEEP_EVERY_S`, `PRUNE_EVERY_S`, `REVERT_PATTERN`, the runner's `batch_size` and `block_ms`, and the Redis key names in `DetectKeys.for_run` are hard-coded. `PRUNE_EVERY_S` slightly affects results (when baselines are forgotten), so it arguably belongs in `wikipulse/config.py` with the other thresholds.
5. **low**, `wikipulse/detect/detector.py:56`: the revert regex misses common summaries such as "Reverting edit(s) by X". It also flags non-reverts that start with "RV" (for example "RV park section" returns `True`), so some edit wars still count and some real edits are dropped.
6. **low**, `wikipulse/config.py:23-61`: `DetectorConfig` has no cross-field validation. A short window longer than the long window only fails later, inside `PageWindows.__init__`, and `rhythm_min > rhythm_max` is accepted silently, which makes the clamp in `Baselines.rhythm` return `rhythm_max` every time.
7. **low**, `wikipulse/config.py:96-113`: every detector default is written twice, in `DetectorConfig` and again as `detect_*` in `Settings`. A test guards against drift, but there are still two sources of truth for each threshold.
8. **low**, `wikipulse/detect/run.py:88`: the public `run()` has no type hint on `should_stop`, against AGENTS.md's "type hints on public functions".
9. **low**, `wikipulse/detect/runner.py:115`: `mkstream=True` means a mistyped `--stream-key` silently creates an empty stream. Without `--exit-when-idle`, the detector then waits forever without any warning.

## Verdict

Verdict: fix

The design is sound and well explained. The event-time watermark, feeding edits to the baseline only after they leave the long window, and the atomic snapshot-plus-`XACK` with PEL replay all hold up, and determinism and crash recovery are demonstrated. Only problem 1 needs fixing before merge, because it makes a spike field the spec requires wrong. The rest are low-severity follow-ups.

## Questions for the human before merging

1. The runner writes changed spikes to Postgres *before* the Redis transaction that saves state and `XACK`s entries. If the process dies between those two steps, what does Postgres contain, what happens on restart, and why does the result still end up correct?
2. Right after a fresh start (`--reset`), why does every page have a baseline of zero for the first hour, which start-rule test does that make irrelevant, and what kinds of pages are therefore more likely to produce false spikes in that hour?
3. A page gets 6 edits from 3 people in 3 minutes, then 25 minutes of silence, then 1 more edit. Is the spike still active after that last edit? Which values of `total_edits`, `last_edit_at` and `ended_at` will it eventually have, and what decides when it ends if the page is never edited again?

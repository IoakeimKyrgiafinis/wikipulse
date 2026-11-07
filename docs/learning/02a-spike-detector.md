# Learning note: Spec 02a, Spike detector

## What was built

A detector that reads the edits from stage 1, finds pages whose editing suddenly becomes
unusual, and stores each spike (start, end, size, strength) in Postgres.

| File | Role |
|------|------|
| `wikipulse/detect/windows.py` | `PageWindows`: per page, the (time, user) of its recent human edits; counts edits and distinct editors in the last 10 and 60 minutes (`counts()`), and hands back edits that leave the 60-minute window (`evict()`, `evict_all()`). |
| `wikipulse/detect/baseline.py` | `DecayingCount` (an exponentially decaying event count) and `Baselines`: each page's normal edit rate, the overall edit rate, and the daily-rhythm factor `rhythm()`. |
| `wikipulse/detect/detector.py` | `SpikeDetector.process(event)`: the start rules, the lifecycle (active, hot, ended), the score, reverts and bots. Pure logic: no Redis, no Postgres, no clock. `SpikeRecord` is what gets stored. |
| `wikipulse/detect/runner.py` | `DetectionRunner`: reads the Redis Stream with a consumer group (`XREADGROUP`), feeds the detector, and every minute writes spikes to Postgres and saves state + `XACK`s in one Redis transaction. Re-reads its pending entries after a crash. |
| `wikipulse/detect/run.py` | `uv run python -m wikipulse.detect.run`: the command line around the runner (`--stream-key`, `--run-id`, `--reset`, `--exit-when-idle`, `--max-entries`). |
| `wikipulse/detect/trending.py` | `uv run python -m wikipulse.detect.trending`: the active spikes of a run, highest score first (`--all`, `--watch`). |
| `wikipulse/db/migrate.py`, `wikipulse/db/migrations/0001_create_spikes.sql` | The migration system: SQL files applied in name order, recorded in `schema_migrations`. |
| `wikipulse/db/spikes.py` | `upsert_spikes`, `active_spikes`, `spikes_for_run`, `delete_run`, `latest_edit_time`. |
| `wikipulse/config.py` | `DetectorConfig` (every threshold) and the matching `DETECT_*` settings; `Settings.detector_config()`. |
| `scripts/determinism_check.py` | The acceptance check: replays a recording twice and compares the spikes. |
| `scripts/compare_thresholds.py` | Runs several threshold variants on a recording, offline, in about a minute. |
| `tests/detect/`, `tests/db/` | 60 unit tests (fakeredis, the recorded fixture, hand-made edits) and 7 integration tests (real Postgres and Redis). |

How data flows:

```
wiki:edits (Redis Stream, from stage 1)
   │  XREADGROUP GROUP detect:live detector-1 ... STREAMS wiki:edits >
   ▼
DetectionRunner ──► SpikeDetector.process(EditEvent)
                        ├─ bots: only move the clock
                        ├─ reverts: counted on a running spike, nothing else
                        ├─ PageWindows.add()  ──evict after 60 min──►  Baselines.record_page_edit()
                        ├─ Baselines.record_overall_edit()  (daily rhythm)
                        └─ start / continue / end the page's spike
   every 60 s (wall clock):
   ├─ changed spikes ──► Postgres `spikes` (upsert)
   └─ MULTI: SET state, HSET changed baselines, XACK processed entries  EXEC
```

Redis keys of one run (`live` by default; replays and experiments use their own `--run-id`):

| Key | Type | Content |
|-----|------|---------|
| consumer group `detect:<run_id>` on the stream | group | position and pending entries of this run |
| `wiki:detect:<run_id>:state` | String (JSON) | windows, active spikes, overall rates, the event-time clock, `last_id` |
| `wiki:detect:<run_id>:baselines` | Hash | page title → `[level, updated_at]` |

## Key concepts

### Stream processing: sliding windows and event time

A *sliding window* answers "how many edits in the last 10 minutes?" at any moment.
`PageWindows` keeps, for each page, a time-sorted list of `(time, user)` for the last 60
minutes. `counts(title, now)` finds the window start with a binary search (`bisect_right`)
and counts what is after it, plus the distinct users. A window is the half-open interval
`(now - 10 min, now]`: an edit exactly 10 minutes old is out (`test_window_is_half_open_at_its_start`).

*Event time* means "now" is never the computer's clock. It is the **watermark**, the newest
`event_time` the detector has seen (`SpikeDetector.now`). Everything time-based uses it:
windows, the baseline decay, the quiet period, even the housekeeping (once per minute of
*event* time `_sweep()` evicts old edits and ends quiet spikes; once per hour of event time
`Baselines.prune()` forgets idle pages). So an hour of recording replayed in two seconds
gives exactly what the live hour gave. The stream is almost, but not perfectly, in time
order, so a slightly late edit is inserted in its place (`insort`); an edit more than
60 minutes behind the watermark goes straight into the baseline (`stats["late"]`).

### State that survives restarts

The detector's memory (windows, baselines, active spikes, the watermark) must survive a
restart, or a restart would forget every burst in progress. `DetectionRunner.snapshot()`
saves it every `DETECT_SNAPSHOT_INTERVAL_S` (60 s):

1. Spikes that changed go to Postgres (`upsert_spikes`).
2. One Redis transaction (`MULTI`/`EXEC`) does three things together: `SET` the state JSON,
   which includes `last_id` (the last stream entry applied to it), `HSET` the page baselines
   that changed since the last snapshot, and `XACK` every entry processed since then.

Page baselines live in a hash and only changed pages are written, because there can be
hundreds of thousands of them; the JSON state stays small (only the last hour of edits).
JSON stores floats with Python's `repr`, which round-trips exactly, so a restored detector
computes bit-for-bit what an uninterrupted one would.

### Redis consumer groups and acknowledgements

A consumer group is a named reader of a stream. `XGROUP CREATE wiki:edits detect:live 0`
creates one positioned at the start. `XREADGROUP GROUP detect:live detector-1 COUNT 500
BLOCK 2000 STREAMS wiki:edits >` returns entries this group hasn't delivered yet, and Redis
adds them to the group's *pending entries list* (PEL) until they are acknowledged with
`XACK`. Two consequences:

- **Independent readers.** Each group has its own position, so the live detector, an
  experiment on the same stream (`--run-id test1`), and later the data-lake writer
  (spec 06) all read every entry. Several *consumers inside one group* would instead split
  the entries between them; that suits stateless work, but not this detector, because all
  edits of a page must reach the same state. So each run is one group with one consumer.
- **Why acknowledgements matter.** An entry is acknowledged only in the same transaction
  that saves the state containing it. If the process dies between snapshots, the entries
  after the last snapshot are still in the PEL. On start, `_replay_pending()` reads them
  again with `XREADGROUP ... STREAMS wiki:edits 0` (id `0` = "my pending entries", not new
  ones), skips the ones whose id is `<=` the saved `last_id`, and processes the rest. Each
  entry changes the state exactly once. Postgres may receive the same spike twice, but the
  write is an upsert keyed on `(run_id, title, started_at)`, so that is harmless.
  `test_crash_and_restart_give_the_same_spikes` kills a runner before it can snapshot and
  checks the restarted one ends with the same spikes as an uninterrupted run.

Try it (PowerShell, with the live detector running):

```
docker compose exec redis redis-cli XINFO GROUPS wiki:edits
docker compose exec redis redis-cli XPENDING wiki:edits detect:live
docker compose exec redis redis-cli HLEN wiki:detect:live:baselines
docker compose exec postgres psql -U wikipulse -c "select title, status, total_edits, distinct_editors, round(score::numeric,1) from spikes where run_id='live' order by started_at"
```

### Anomaly detection: baselines, cold start, daily rhythm

**Moving averages and z-scores.** A baseline is "how much is normal". `DecayingCount` is a
continuous-time exponential moving average: each edit adds 1 to a *level*, and the level
shrinks by `e^(-dt/tau)` as time passes (tau = `DETECT_BASELINE_TAU_HOURS` = 24 h). The
rate is `level / tau`. It needs two numbers per page, updates lazily, and the result
doesn't depend on the order edits were fed in (`test_order_of_events_does_not_change_the_level`).

A *z-score* says how unusual a value is: how many standard deviations above the mean. For
counts of random independent events the natural model is Poisson, where the standard
deviation is the square root of the mean, so `poisson_z(observed, expected) =
(observed - expected) / sqrt(expected)`. 15 edits where 0.5 are expected gives z = 20.5;
6 edits where 5 are expected gives 0.4. The spike's `score` is the highest z it reached.

**The baseline must not learn from the burst.** If edits went into the baseline as they
arrive, a burst would raise its own "normal" while it happens. Here an edit reaches the
page's baseline only when it *leaves the 60-minute window* (`_feed_baseline()` and
`_sweep()` call `record_page_edit()` with evicted edits). So the baseline is "the page's
rate up to an hour ago".

**Cold start.** Most pages have no history, and when the detector starts, none have. Two
things handle it:
- Minimum counts (`min_edits`, `min_editors`) are always required. A page going from 0 to
  3 edits gives a huge ratio but is not news.
- The ratio test (`min_ratio` x expected) uses the baseline as it is, so for a page without
  history (expected 0) only the minimum counts decide. The score uses the expected count
  raised to `expected_floor` (0.5 per 10 minutes), so it stays finite.
- A decaying average that has seen only one hour underestimates the rate 24-fold. `_rate()`
  divides the level by the *observed* time, `tau * (1 - e^(-observed/tau))`, instead of
  `tau`: about `observed` for short runs, `tau` for long ones
  (`test_cold_start_correction_gives_the_plain_average_for_short_runs`).

**Daily rhythm.** Wikipedia is busier at some hours. `Baselines.rhythm()` divides the overall
human edit rate of the last hour (tau 1 h) by the rate over the baseline period (tau 24 h),
clamped to [0.5, 2]. A page's expected count is its own baseline times this factor, so at a
busy hour a page needs more edits to look unusual, at night fewer.

**Distinct editors and reverts.** Two design notes from the spec, decided like this:
- One person making 20 edits in 10 minutes is someone working on an article
  (`test_one_busy_editor_is_not_a_spike`). Both start rules require several *different*
  editors, and the spike records `distinct_editors`.
- Edits whose summary starts with "Reverted", "Undid", "Undo", "Revert", "Restored
  revision" or "rv"/"rvv" (`is_revert()`) don't count toward detection. A vandal plus the
  patrollers reverting them creates a burst with several editors that is not news. On the
  recording, counting reverts adds two spikes, *Dying Light 2* and *Food Cube Rule*, both
  revert-heavy edit wars (see Results). A running spike still counts its reverts in
  `revert_edits`, which later stages can use. `DETECT_COUNT_REVERTS=true` switches this off.

### The spike lifecycle

```
no spike ──start rule──► ACTIVE ──edit, short window still ≥ 2 edits ("hot")──► ACTIVE
                           │
                           └── 30 min (event time) without a hot moment ──► ENDED
```

- **Start**, either rule, both with at least `min_ratio` (5) times the expected count:
  - short window: at least 5 human edits from at least 3 people in 10 minutes;
  - long window: at least 10 edits from at least 4 people in 60 minutes, for stories that
    build up more slowly (`test_a_story_building_over_an_hour_starts_a_spike_on_the_long_window`).
- **Continue**: every human edit is added to the spike. An edit after which the 10-minute
  window still holds at least 2 edits is a *hot* moment. Starting needs much more than
  staying alive (hysteresis), so one burst with a lull stays one spike
  (`test_a_short_lull_keeps_one_spike`).
- **End**: 30 minutes without a hot moment. `ended_at` is the spike's last counted edit.
  The check runs both in the minute sweep (so a spike ends even if its page gets no more
  edits) and right before each edit of the page (so the exact moment doesn't depend on when
  the sweep ran). A new burst after that is a new spike.

Each spike records `title`, `started_at` (when a start rule first held), `first_edit_at`
(oldest edit of the triggering window, for spec 02b's time to detection), `last_edit_at`,
`ended_at`, `total_edits`, `distinct_editors`, `revert_edits`, `peak_edits_10m` and `score`.

### Iterating with data

The loop the spec asks for: change one threshold, replay, compare. Two ways:

- Faithful: `stream.replay --file <rec>` then `detect.run --stream-key wiki:replay:edits
  --run-id try1 --reset --exit-when-idle 10`, then `detect.trending --run-id try1 --all`.
- Fast: `scripts/compare_thresholds.py --file <rec> --set min_edits=4 --set
  count_reverts=true` runs the same detector on the same events (stage 1's filter and
  dedup, no Redis) for several variants and prints the spikes of each. An hour of
  recording takes about a minute, most of it parsing the 235 MB file.

Because of the event-time rule both give the same spikes.

## Design decisions and deviations

- **Times are floats inside `wikipulse/detect/`** (seconds since 1970, from
  `event_time.timestamp()`), converted to `datetime` only in `SpikeRecord`. Simpler maths
  and smaller snapshots; nothing reads the clock.
- **A second start rule on the 60-minute window** (`long_min_edits` = 10,
  `long_min_editors` = 4). The spec lists 10- and 60-minute counts; with only the 10-minute
  rule, a story edited steadily (say 12 edits from 4 people in an hour) would never start.
- **The ratio test uses the raw baseline, the score uses the floored one.** First I floored
  both, but then the long rule effectively needed 15 edits (5 x the floor of 3 per hour),
  not 10. Separating them makes the roles clear: minimum counts handle cold start, the ratio
  handles pages with real history, the floor only keeps the score finite.
- **Edits reach the baseline one hour late** (when they leave the long window), so a burst
  can't raise its own baseline. Consequence: for the first hour after a fresh start every
  page's baseline is zero and only the minimum counts decide (cold start).
- **Cold-start correction** in `_rate()` (divide by the observed time, not tau), with at least
  one long window of assumed observation per page so one early edit isn't read as a huge rate.
- **Reverts are excluded from detection** but counted on running spikes (`revert_edits`, an
  extra column the spec didn't ask for). Detection by edit-summary prefix only: tags such as
  `mw-undo` and `mw-rollback` would be more reliable, but stage 1's `EditEvent` doesn't carry
  them (changing stage 1 is out of scope here).
- **Bots** are excluded from everything except moving the watermark.
- **Lifecycle end** = 30 minutes without a hot moment; `ended_at` = the last counted edit.
  No maximum duration: a story that stays hot for a day is one spike.
- **`run_id`**: one id names the consumer group (`detect:<run_id>`), the Redis state keys
  and the spikes' `run_id` column, so live, replays and experiments never mix. The table's
  unique key is `(run_id, title, started_at)`. Spikes still active when a replay ends stay
  `active` (the data simply ends; nothing is invented).
- **One consumer per group**, deliberately (see "Redis consumer groups"). A new group starts
  at `0` (the whole retained stream) by default; `--start-id '$'` starts with new entries.
- **Snapshot timing is wall clock** (every 60 s), because it only decides how much work a
  crash repeats, never what is computed. Postgres is written at the same moments, so
  `trending` can lag the detector by up to a minute.
- **Synchronous runner** (redis-py and psycopg, no asyncio): the loop is "read a batch,
  process, sometimes snapshot", with nothing to do concurrently. `XREADGROUP BLOCK 2000`
  returns at least every 2 s, so Ctrl+C (handled by `StopFlag`) is noticed quickly.
- **Migrations** take a Postgres advisory lock, so two migrators started together take turns.
- **Extra tools not in the spec**: `scripts/determinism_check.py` (the acceptance check,
  re-runnable) and `scripts/compare_thresholds.py` (the fast iteration loop).
- **CI** now also starts a `postgres:16-alpine` service and runs the migrations before the
  integration tests. Not validated with actionlint; it runs on the next push.
- **No new dependencies.** fakeredis (already a dev dependency) supports consumer groups,
  so the runner is unit-tested without Docker.
- **Live runs used the placeholder User-Agent** from `.env` (the stage-1 consumer warns about
  it). Set your real contact in `.env` before running the consumer for long.
- **Redis cleanup before the live run**: the stage-1 checkpoint `wiki:consumer:last_id` was
  11 hours old, and resuming from it would have downloaded 11 hours of backlog from
  Wikimedia. It was deleted (the documented way to start fresh), so the live consumer
  started at "now". The 303 old entries in `wiki:edits` stayed and were read by the detector
  first (in the looser run they produced an *Ana Matronic* spike at 01:37, which ended at
  once when the watermark jumped to the present).
- **`trending` on Windows**: printing a title like *Boban Marjanović* crashed when the
  output was redirected (Windows then uses the cp1252 code page). `main()` now tells stdout
  to replace characters it can't encode.

## Results

All measured on 2026-11-07.

**Determinism (`uv run python scripts/determinism_check.py --file
data/recordings/history-20261106T2000Z.jsonl`).** The recording is one hour of the full
stream (2026-11-06 20:00 to 21:00 UTC, 141,149 messages, 235 MB). Each replay kept 7,215
English Wikipedia article edits (2,996 bot edits, 175 reverts, 4,044 counted human edits).
Run A detected without interruption; run B stopped after 3,607 entries, saved its state,
and resumed for the remaining 3,608.

| Config | Spikes A | Spikes B | Result |
|--------|---------:|---------:|--------|
| defaults | 1 | 1 | PASS, identical in every field |
| looser (`DETECT_MIN_EDITS=3 DETECT_MIN_EDITORS=2 DETECT_LONG_MIN_EDITS=6 DETECT_LONG_MIN_EDITORS=2`) | 28 | 28 | PASS, identical in every field |

`scripts/compare_thresholds.py` (offline, no Redis) found the same 1 and 28 spikes, as the
event-time rule predicts. Each replay took about 2.5 minutes, detection about 2 seconds.
When run B stopped, 393 entries of its last batch had been delivered but not processed;
the restart logged `Re-reading 393 pending entries` and processed them from the PEL.

**Iterating on the recorded hour** (`compare_thresholds.py`, cold start, so only the
minimum counts decide):

| Variant | Spikes | Notes |
|---------|-------:|-------|
| defaults (short 5 edits / 3 editors, long 10 / 4) | 1 | *Syed Soharwardy* (7 edits, 5 editors) |
| `min_edits=4` | 3 | adds *Trevor, Wisconsin* and *Zach Dembo* (4 edits each); Soharwardy detected 7 minutes earlier |
| `count_reverts=true` | 3 | adds *Dying Light 2* and *Food Cube Rule*: revert-heavy edit wars, the case the revert rule exists for |
| `long_min_edits=8,long_min_editors=3` | 2 | adds *Food Cube Rule* via the long window |
| short 3/2, long 6/2 | 28 | many plausible pages (*2026 Pacific hurricane season*, *Susie Wiles*, US Senate elections) and much noise |
| `quiet_period_min=10` | 1 | the same spike, ending earlier with 5 instead of 7 edits |

It was a quiet Friday evening hour. The defaults are deliberately strict (precision first);
spec 02b measures them against the "Current events" portal and decides what to change.

**Live run** (2026-11-07 12:45 to 13:31 UTC, 45 minutes; the stage-1 consumer and two
detector runs on `wiki:edits` at the same time, each its own consumer group): 4,481 edits
(1,583 by bots, 90 reverts). `uv run python -m wikipulse.detect.trending` at the end:

```
Spikes of run 'live', detector time 2026-11-07 13:30:21 UTC
 score edits editors peak/10m reverts started (UTC) duration  title
  10.6    12       3        8       0 11-07 13:20    0h15m  Dixons Brooklands Academy
   7.8     8       3        6       0 11-07 13:00    0h38m  Ramayana: Part 1
   6.4     7       3        5       0 11-07 13:27    0h13m  Results of the 2026 New Zealand general election
```

New Zealand voted on 7 November 2026, so its results page is exactly the kind of page the
detector should find. *Ramayana: Part 1* is a film and the school a local topic; whether
those were news or just busy editing is what 02b's labels will tell. The looser run
`live-loose` had 17 spikes in the same 45 minutes, among them *2026 New Zealand general
election* itself (5 edits from 5 editors, but never 5 within 10 minutes, so the defaults
missed it), *Douglas Preston*, *Grand-Place*, *Caffeine pouch* and *2026 Men's Rugby League
World Cup final*, but also *Food Cube Rule* again. Sensible pages in both lists; the
looser one finds more news at the cost of more noise, the trade-off 02b will quantify.

**Restart.** The live detector was killed hard (`timeout`) at the end. Restarted, it
logged `Restored state: last entry 1794058247378-0, 3 active spikes, 177 page baselines`
and showed the same three spikes. The crash case with unacknowledged entries is tested by
`test_crash_and_restart_give_the_same_spikes` (fakeredis) and
`test_crash_between_snapshots_loses_and_doubles_nothing` (real Redis).

**Tests**: 132 unit tests pass (`uv run pytest`, 60 of them new), 12 integration tests
pass (`uv run pytest -m integration`, 7 new). `ruff check` and `ruff format --check` are
clean. Live state is small: the state JSON (mostly the last hour of edits) was 136 KB
after 45 minutes, plus a hash of 177 page baselines.

## Check your understanding

1. A page has been edited about 30 times an hour all day. Between 14:00 and 14:10 it gets
   12 edits from 4 people. Does a spike start with the default settings (ignore the daily
   rhythm)? What if the page had no history at all?
2. Why does an edit enter a page's baseline only when it leaves the 60-minute window, and
   what does that cost when the detector has just started?
3. The detector process is killed 50 seconds after its last snapshot. Which stream entries
   are processed again after the restart, how does the runner find them, and why doesn't
   that create duplicate spikes in Postgres?

<details>
<summary>Answers</summary>

1. Expected in 10 minutes: 30/h x 1/6 h = 5 edits. The short rule needs 5 edits, 3
   editors *and* at least 5 x 5 = 25 edits; 12 is not enough, so no spike (that page is
   busy every day). The long rule would need 5 x 30 = 150 edits in the hour. Without
   history the expected count is 0, the ratio test passes, and 12 edits from 4 people meet
   the minimum counts: a spike starts (cold start).
2. So a burst can't raise its own "normal" while it is happening. If edits went in at once,
   a page with 30 edits in 20 minutes would quickly look normal to itself. The cost: for
   the first hour after a fresh start every baseline is zero, so only the minimum counts
   decide, and busy pages can produce spikes they wouldn't produce later.
3. Every entry after the `last_id` saved in the last snapshot. They were delivered but
   never `XACK`ed, so they are still in the group's pending entries list; `_replay_pending()`
   reads them with `XREADGROUP ... STREAMS wiki:edits 0` and skips any at or before
   `last_id`. Re-processing them creates the same spike records (same events, same order,
   same state), and `upsert_spikes` writes them with `ON CONFLICT (run_id, title,
   started_at) DO UPDATE`, so the rows are overwritten, not doubled.

</details>

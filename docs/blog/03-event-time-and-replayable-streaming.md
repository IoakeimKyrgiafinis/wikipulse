# Event time: why a replayed day gives exactly the live results

*Draft. Built from the learning notes of specs 01, 02a, 03b, 06a and 10.*

WikiPulse processes Wikipedia's live edit stream: about 1.7 English Wikipedia article
edits per second, out of 32 to 39 messages per second for all wikis. A spike detector
counts edits in sliding windows, a grouper turns spikes into stories, and both decide
things like "this spike has been quiet for 30 minutes, so it has ended". One rule from
the first day shaped everything after it: **processing logic never reads the clock.**

## Two clocks

Every edit carries its own timestamp, the moment Wikipedia recorded it: the **event
time**. The consumer also notes when it received the message, but that is metadata.
Everything that decides something uses event time:

- `SpikeDetector.now` is the **watermark**, the newest event time the detector has seen.
  The 10- and 60-minute windows, the decay of each page's baseline, the 30-minute quiet
  period that ends a spike, and even housekeeping (`_sweep()` once per minute of *event*
  time, pruning idle pages once per hour of event time) all run on it.
- The grouper closes events and advances its own clock by the newest `last_edit_at` of
  the detector's spikes, never by `datetime.now()`.
- "Today" and "this week" in a question to the knowledge graph mean relative to the
  newest event in the graph, not to the calendar.

The detector itself is pure logic: `SpikeDetector.process(event)` takes an edit and
changes state; no Redis, no Postgres, no clock. That makes it trivially testable, and it
makes the next property possible.

## Replays are not simulations

Because nothing reads the clock, an hour of recorded edits replayed in two seconds gives
exactly what the live hour gave. This isn't a nice-to-have; nearly every result in
WikiPulse depends on it:

- **Determinism, checked.** `scripts/determinism_check.py` replays a recording twice, one
  run stopped half way and restarted from its snapshot. On an hour of the full stream
  (141,149 messages, 7,215 kept edits) both runs gave identical spikes in every field,
  with default and with looser thresholds. Grouping was checked the same way: a replay
  stopped at noon and restarted gave the same 665 assignments as an uninterrupted one.
- **Evaluations run the production code.** Two full recorded days are detected in about
  20 seconds per threshold setting, so comparing four settings costs a minute, not two
  days of waiting.
- **The CI eval gate** replays 16 committed hours on every pull request in about two
  seconds.
- **The quickstart** replays a bundled day through the real replay, Redis Stream,
  detector and grouper at 300 times real speed. It produced the same 74 spikes of
  5 November that the detector produces offline on the same edits: a replay over Redis
  with consumer groups, snapshots and process boundaries, and a plain loop over a list,
  agree exactly.

## What it costs

Event time is not free.

- **Time only moves when data arrives.** If the stream goes quiet, the watermark stops,
  and a spike that should have ended stays "active" until the next edit. On the live
  stream that is a fraction of a second; at the end of a replay it means the last spikes
  of the day stay active, and the page says "data up to Nov 5, 23:58 UTC" so a reader
  can interpret "active".
- **Late data needs a rule.** The stream is almost, but not perfectly, in order. A
  slightly late edit is inserted in its place in the window; one more than 60 minutes
  behind the watermark goes straight into the baseline and is counted as late.
- **Starting from a jump.** After a pause, the live consumer resumes from its checkpoint
  and the watermark jumps to the present; old entries still in the stream were once read
  first and produced a spike that ended at once when the watermark jumped.
- **Wall-clock time still exists, for operations only.** Snapshot intervals, API cache
  ages and polling are wall-clock decisions, because they are about the machine, not the
  data. Keeping that line sharp is the discipline.

## Exactly-once results from at-least-once delivery

Replaying also means processing the same edit more than once, by design or after a crash.
The pipeline is built for at-least-once delivery with idempotent results:

- The consumer writes an edit, its deduplication key and the stream checkpoint in one
  Lua script, so a crash can't leave the edit written without the checkpoint or the
  other way round. The crash test (killed hard mid-stream, restarted a minute later)
  found 487 kept events in the recording and 487 in Redis: 0 missing, 0 extra, 0
  duplicates; the 87 events that arrived while it was dead were recovered.
- The detector acknowledges stream entries in the same Redis transaction that saves its
  state, and stores spikes as upserts keyed on run, title and start.
- The lake archiver writes an hour as a Parquet file merged by `event_id` and only then
  acknowledges, so an hour rewritten after a crash has no gaps and no duplicates.

## The takeaway

Choose event time on day one. It turns every recording into a test case, every
evaluation into a run of the real code, and every crash into a replay. The price is a
handful of explicit rules for silence, lateness and jumps, which you have to write down
anyway once a stream meets the real world.

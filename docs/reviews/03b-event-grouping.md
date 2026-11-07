# Review: 03b-event-grouping

Branch `spec-03b-event-grouping`, one commit (`315a536 Spec 03b: event grouping`) against `main`.
30 files changed: the new `wikipulse/events/` package (similarity, grouper, embed, run, feed), `wikipulse/db/events.py`, migration `0004_create_events.sql`, `wikipulse/eval/grouping.py` + `grouping_labels.py`, config/.env.example/README, 41 tests, pair labels and three eval reports, and `pyproject.toml`/`uv.lock` (sentence-transformers, CPU-only torch).

## Acceptance criteria

| Criterion | Result | Evidence |
|---|---|---|
| Unit tests for the similarity score, assignment, merging and the metrics | pass | `tests/events/test_similarity.py` (9 tests: link, hub, time, embed floor, same page, symmetry, weights), `tests/events/test_grouper.py` (11: join, new event, merge into oldest, ending, title, determinism, restore), `tests/eval/test_grouping.py` (strata, sampling, precision, stratified recall, labels). `uv run pytest`: 263 passed, 20 deselected. `uv run pytest -m integration`: 20 passed (includes `tests/events/test_events_db.py`, which covers grouping, restart and ending against real Postgres with a fake encoder). |
| A replay of a recorded day produces an event feed; report number of events and pages per event | pass (with a caveat, see problem 2) | I ran `uv run python -m wikipulse.events.run --spike-run 02b-loose --run-id review-03b --since 2026-11-05 --until 2026-11-06 --once --reset`: 36 s, "Grouped 665 spikes: 563 new events, 102 joined an event, 1 merges" (17 active). The spike-to-event membership was **identical** to the stored `03b-default` run for all 665 spikes, which supports requirement 1 (deterministic). `wikipulse.events.feed --run-id review-03b --all` printed "562 events (17 active, 545 ended), 665 pages. Pages per event: mean 1.18, median 1, max 16", the same as the learning note. I deleted the review run afterwards. |
| Requirement 1: deterministic for the same input order | pass | Matches the replay above. Ordering is `started_at, title, id`. Cosines are rounded to 4 decimals. |
| Requirement 2: an event ends when all its spikes have ended | pass | `Event.is_over` / `Grouper.advance`; covered by `test_an_event_ends_when_all_its_spikes_are_over` and the integration test. Postgres check: for every non-merged event in all three runs, `page_count` equals its `event_pages` count, and no `event_pages` row points at a merged event. |
| Requirement 3: provisional title is the highest-scoring page | pass | `Event.refresh`; `test_the_title_is_the_highest_scoring_page`; the feed titles match the top-scoring page. |
| Outputs: similarity.py, grouper.py, migration, `events.run`, `events.feed`, `eval/grouping.py` with LLM labeling + `--manual` | pass | All present. LLM labels in `evals/labels/grouping_labels.jsonl` all have `labeler: llm`, a model and (for v2) `prompt_version`. No human labels exist, so none were overwritten. |
| Ruff | pass | `ruff check .`: all checks passed. `ruff format --check .`: 152 files already formatted. |
| Dependencies / Linux CI | pass | The spec names `sentence-transformers`, so it is allowed. torch is its required engine, listed only so it can be pinned to the CPU index. `uv lock --check` passes. The lock has `torch 2.15.0+cpu` wheels for manylinux x86_64/aarch64 and win_amd64 (plain 2.15.0 for macOS arm64), with 0 `nvidia-*` packages and no triton. Unit and integration tests use fake encoders, so CI never downloads the model. I did not run CI on Linux myself. |
| No real email in committed files | pass | I grepped the full diff for email-like strings and found none. |

## Problems

1. **medium**, `wikipulse/config.py:87-91` (and `wikipulse/events/similarity.py:214-224`): A direct link plus close timing clears the join threshold with no help from summary similarity (0.4·1 + 0.2·t ≥ 0.55 whenever the spikes start within 90 minutes). The hub rule at 1000 links does nothing for the main hub: *Deaths in 2026* has 387 links, while the median page has 149. Under single linkage this chains unrelated deaths into one event (Herbert Blomstedt / Kimmarie Johnson / Ali-Asghar Fani at joined ≈ 0.56-0.58, and a 21-page multi-death event in `03b-close180`). The learning note acknowledges this, but the defaults ship with it.
2. **medium**, `wikipulse/events/feed.py:37` and `wikipulse/eval/grouping.py:224`: "Pages per event" counts spikes, not distinct pages. In the default run, 12 of the 47 "multi-page" events are a single page spiking more than once (124 distinct pages vs 150 spikes in multi-page events; *Ramayana: Part 1* shows as "5 pages" with 2 distinct pages). So the numbers reported for the acceptance criterion and in the learning note overstate how much grouping happened, and the feed lists the same page several times.
3. **low**, `wikipulse/eval/grouping.py:183`: The "Recall, estimated over all pairs" row is just the candidate recall, because the far stratum's estimate is 0/24. The report's own rule-of-three bound allows about 27,000 same-story far pairs, so the headline recall has no meaningful lower bound with a 25-pair far sample. The caveat appears below the table, not in it.
4. **low**, `wikipulse/config.py:94`: `EVENTS_SPIKE_CLOSE_MIN` is documented as "the detector's quiet period" but is an independent setting next to `DETECT_QUIET_PERIOD_MIN`, so the two can silently drift apart.
5. **low**, `wikipulse/eval/grouping_labels.py:88`: `pick_pair_label` takes the latest LLM label whatever its `prompt_version`. A pair that only has a v1 label still counts in a report that is run without relabeling. Today all 167 chosen labels are v2, so nothing is affected yet.
6. **low**, `wikipulse/events/similarity.py:20`, `embed.py:15`, `eval/grouping.py:40`: The package imports `numpy` directly but `pyproject.toml` doesn't declare it; it is only available transitively through sentence-transformers.
7. **low**, `wikipulse/events/run.py:173`: With `--until`, the clock is capped, so a finished replay permanently leaves 17 events `active`. The default feed (active only) shows them as running until "now". This is documented, but surprising.
8. **low**, `wikipulse/db/migrations/0004_create_events.sql:31`: `ON DELETE CASCADE` from `spikes` means deleting or re-running a detector run silently empties `event_pages` while the `events` rows stay, with stale `page_count`/`title`.
9. **low**, `wikipulse/events/embed.py:54`: Every run that loads the model contacts the Hugging Face Hub (an "unauthenticated requests" warning on each replay), even though the model is cached. Neither `.env.example` nor the README says how to run offline (`HF_HUB_OFFLINE=1`).
10. **low**, `tests/events/test_grouper.py:85`: The determinism test runs identical code twice in one process. It cannot catch the realistic risk, which is embedding values that differ with batch composition between a live run and a replay. My replay comparison did confirm determinism end to end.
11. **low**, `wikipulse/events/grouper.py:43` / `feed.py`: `joined_score` is 1.0 both for a repeat spike of the same page and for the founding spike of an event that was later merged (e.g. *Cindy Hyde-Smith* in event #2). So the feed's "joined 1.00" means two different things.
12. **low**, `wikipulse/eval/grouping.py:69-70`: The sampling sizes and `NEAR_COSINE = 0.65` are module constants, not settings. This follows the precedent set by `eval/label.py`, but `NEAR_COSINE` depends on the embedding model and would need retuning if `EVENTS_EMBED_MODEL` changes.

Nothing is out of scope: there are no LLM-generated titles, no edits to the spec, devlog or `.gitattributes`, no existing tests were changed or weakened, and nothing under `data/` was committed. Processing logic uses event time only. The only wall-clock uses are label/report metadata, `updated_at`, and the live enrichment wait, all documented.

Verdict: fix

Problem 2 is a small fix but it corrects the numbers reported for an acceptance criterion: count distinct pages, or label the metric as spikes, and update the learning note's figures. Problem 1 is a tuning trade-off that the human should consciously accept or change (for example a lower `EVENTS_LINK_HUB_SIZE`, or requiring some embedding signal before a link counts) before the defaults go live.

## Questions for the human before merging

1. Spike C links to page A (event 1) and has a similar summary to page B (event 2). Both pair scores reach 0.55. What happens to events 1 and 2, which number survives, and what will `events.feed` show for the losing event afterwards?
2. Why can't the grouper ever put the 10:00 spike of *Lloyd Geering* and the 15:00 spike of *Deaths in 2026* into one event, whatever weights you choose? Which setting did the `03b-close180` run change to allow it, and what did that cost in precision?
3. The default report says recall is 23.7% "over all pairs". Where does that number come from (which strata and weights), and why does the rule-of-three line under the table mean the true recall could be far lower?

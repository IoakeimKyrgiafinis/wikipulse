# Review: 04b Knowledge graph extraction

Branch `spec-04b-graph-extraction`, commits `23435db` (WIP) and `420f647` (finish), reviewed against `main`.
Reviewed on 2026-11-08 by an independent reviewer.

## How it was checked

- Read `AGENTS.md`, the spec, the full diff (27 files) and the learning note.
- `uv run pytest`: 349 passed, 32 deselected. `uv run pytest -m integration` (Docker Postgres + Neo4j): 29 passed. `uv run ruff check .`: clean. `uv run ruff format --check .`: 190 files already formatted.
- Checked the stored results (grouping run `03b-default-fix`) in Postgres (`graph_extractions`) and in Neo4j (cypher-shell). I did not re-run the big extraction.
- Ran the CLI over the already-done 19:00 to 23:00 window: 0 events pending, 0 LLM calls, 0 Wikimedia requests. Report printed.
- Re-extracted one event (`03b-default-fix:341`, Ueno Bank) with `--force --limit 1`: 16 entities (9 linked), 9 relations, 27.1 s. Afterwards the graph invariants still held. **Note:** this run replaced event 341's part of the human's graph with a new LLM answer. The old answer had 12 entities; the title is unchanged.
- Searched the diff for the real e-mail address, `data/` files and `.env`: none found.

## Acceptance criteria

| Criterion | Result | Evidence |
|---|---|---|
| Unit tests with a mocked LLM: parsing | pass | `tests/graph/test_extract.py` (8 tests, `ScriptedClient`): schema enums, source/certainty to provenance, merging duplicates, relation-only entities, dropping generic nouns and dates, a retry on an invalid answer. |
| Unit tests: linking order | pass | `tests/graph/test_link.py::test_linking_tries_pages_then_links_then_search` checks which method linked each name and the order of requests (`query/pageprops`, then `wbsearchentities` only for "Bend"). `test_a_name_found_on_the_event_pages_needs_no_request` checks that a page match makes no request. |
| Idempotent writes against a test Neo4j (`integration`) | pass | `tests/graph/test_store_neo4j.py` (6) and `tests/graph/test_run_db.py` (3). All 29 integration tests pass. Writing the same event twice gives the same graph. Re-processing replaces facts and removes orphans. |
| A replayed day produces a graph; report events, entities, linked share, average time per event | pass (on a sample) | Only 233 of the day's 572 events were extracted (16:00 to 23:00), under the 60-minute rule; the learning note says so. Postgres, window 19:00 to 23:00: 132 `ok` rows, average 9.39 s per event (LLM 6.74 s), 840 entity mentions, 680 linked (81%), 497 relations. These match the note. Neo4j: 233 `Event` nodes, 1,280 entities with 1,280 distinct keys, 1,003 Wikidata IDs on 1,003 nodes, 0 duplicate `INVOLVED_IN`/`RELATED_TO`, 0 orphan entities or pages, 0 `error` rows. |
| Req 1: idempotent writes | pass | Integration tests above. On real data, `--force` on event 341 kept 233 events, distinct keys equal to node count, and 0 orphans. |
| Req 2: one entity per Wikidata ID | pass | Uniqueness constraints on `Entity.key` and `Entity.wikidata_id`. Neo4j has 1,003 distinct IDs on 1,003 nodes. Aliases are merged (e.g. "Benjamin Netanyahu" / "Netanyahu"). |
| Req 3: failures logged per event and retried later | pass | `test_a_failed_event_is_recorded_and_retried_later`. The note documents 3 real failures that later succeeded on retry. |
| Outputs (compose service, modules, prompt, CLI, Event titles/descriptions, `EXTRACT_MODEL`) | pass | `wikipulse-neo4j-1` is healthy. All listed files exist. Every Event node has a title and description (0 missing). `extract_model` is in `config.py` and `.env.example`. |
| Out of scope (question answering) not touched | pass | No QA code. |

## Problems

1. **medium**, `wikipulse/db/graph.py:20`: the event's `fingerprint` is only its list of spike ids. Spikes keep their id while they grow (`ON CONFLICT ... DO UPDATE` in `db/spikes.py`), and nothing re-extracts when the event changes from `active` to `ended`. So in live mode an event is extracted the moment it appears, from the diff of the first minutes of its burst. After that its title, entities, `status` and `ended_at` in Neo4j stay stale unless another spike joins. Live mode is the spec's main mode ("processes events as they appear").
2. **medium**, `wikipulse/graph/run.py:196`: `remove_merged()` only removes events whose status is `merged`. When events are deleted from Postgres (e.g. `wikipulse.events.run --reset`), the cascade deletes their `graph_extractions` rows, but their `Event` nodes stay in Neo4j. Event numbers the new grouping doesn't reuse remain as phantom events, and `run_counts()` counts them.
3. **low**, `wikipulse/graph/run.py:60` and `wikipulse/db/graph.py:38`: the retry delay (10 min) and the backoff cap (2^5) are hard-coded operational settings. They belong in `wikipulse/config.py` next to `GRAPH_POLL_INTERVAL_S`.
4. **low**, `wikipulse/graph/run.py:62`: `EVENT_ERRORS` includes `KeyError`, `TypeError` and `ValueError`, so programming bugs are recorded as retryable event failures with backoff, which hides them. Meanwhile an `AttributeError` (e.g. `parse_revisions(None)` when a response body is `None`, `wikipulse/graph/inputs.py:258-259`, where `link.py` guards with `or {}`) would crash the whole runner.
5. **low**, `wikipulse/graph/inputs.py:31`: the comment says "the diff spans all of them anyway", but revisions come back newest first with `rvlimit=50`. For a burst of more than 50 revisions the diff starts at the 50th-newest revision's parent and misses the earlier additions.
6. **low**, `wikipulse/graph/store.py:95-97`: a node's `name` and `link_method` are whichever write came last, and `aliases` only grow. A re-extraction leaves stale aliases, and `name`/`by_method` depend on processing order. This is the same kind of order dependence that the type vote (`RETYPE`) fixed for `type`.
7. **low**, `wikipulse/graph/inputs.py:258`: when no revision is found in the burst window, the page's facts get `source_revision = NULL`. Neo4j has 5 `INVOLVED_IN` and 2 `RELATED_TO` without a revision (e.g. event 428, "2026 Afyonkarahisar Murders"), although the spec says every relationship stores one.
8. **low**, `wikipulse/graph/run.py:99`: `graph_summary()` always reports the whole grouping run, even when `--since/--until` limit the window, so the second report line doesn't describe the same events as the first. This is confusing when reading a sample's results.
9. **low**, `wikipulse/graph/link.py:245`: all Wikidata searches of an event start at once with `asyncio.gather` (limited only by the client's concurrency). The note reports 24 HTTP 429 responses during the run. Searching one at a time would be politer to Wikimedia, per AGENTS.md.
10. **low**, `wikipulse/graph/names.py:41`: `is_proper_name()` accepts amounts that start with a capital letter. "US$ 57.6 million" is an `Entity` in the graph, although the prompt says to leave out numbers.
11. **low**, `docker-compose.yml:39`: the image tag `neo4j:5-community` floats, while `RETYPE` uses the `CALL (n) { ... }` scoped-subquery syntax, which needs Neo4j 5.23 or later. Pinning a minor version would make the requirement explicit.
12. **low**, `docs/learning/04b-graph-extraction.md:288`: the note says the acceptance run covers "131 events", but its results table (line 314) and Postgres both say 132.

Verdict: fix

Problems 1 and 2 affect the live mode the spec asks for: events go stale or become phantom events in the graph. The replayed-day path, the tests and the graph invariants are sound.

## Questions for the human before merging

1. An event appears in live mode with one spike that is still growing. It is extracted right away. Twenty minutes later the same spike has 30 more edits and the event has ended. What does the graph show for this event, and which column of `graph_extractions` would have to change for it to be extracted again?
2. Event 12 mentions "Trump" (linked by search to Q22686), and event 40 mentions "Kaylub Giamanco" (unlinked). Both events are later re-extracted with `--force`. Trace what `write_event()` deletes and re-creates for each entity node, and explain why the Q22686 node survives even if event 12's new answer no longer mentions Trump.
3. The learning note found that `--force` on the same events gives different LLM answers although the input message is byte-for-byte identical. Which parts of the project's "event time, not wall-clock time" guarantee still hold for this stage, and what would you need to store to make a replay reproduce the graph exactly?

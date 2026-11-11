# Review: 10 Polish and presentation

Branch `spec-10-polish` (9 commits, `5e6d322` to `45ef658`) compared with `main`.
Static review plus `pytest`, `ruff` and an AST comparison of every changed package file.
The full quickstart was not rerun (Docker may be down; the caller asked for a static review).

## Acceptance criteria

| Criterion | Result | Evidence |
|---|---|---|
| On a clean machine with Docker, uv and Ollama, the quickstart works (fresh clone) | pass (default path), not verified (`--graph`) | Not rerun by the reviewer. The learning note records four fresh-clone runs; the last two (fresh volumes, own Compose project and ports) passed in about 7 min with 74 spikes and 69 events. `uv run pytest tests/test_quickstart.py`: 8 passed (sample anonymous and under 3 MB, every Wikimedia URL setting overridden, recording round trip, existing pages kept, every spike of the day has its page). The learning note says `--graph` has **never** run end to end. |
| CI is green on the final commit | pending | `gh run list`: run 43039162438 on `45ef658` (the HEAD), `test`, `eval-gate`, `terraform`, `dags` succeeded; `smoke` still in progress at review time. The learning note says CI wasn't checked, yet the spec status is already `done`. |
| Definition of done: tests and ruff | pass | `uv run pytest`: 1294 passed, 1 skipped. `ruff check .`: all checks passed. `ruff format --check .`: 463 files already formatted. |

Focus checks:

| Check | Result | Evidence |
|---|---|---|
| Numbers in README, blog and architecture trace to a note, eval or review | mostly pass | Checked: the detection table (02b note lines 216-221), 11 of 52 and ±14 pp, gate 0.526/0.211/38/180/240 KB (09c), grouping 665/572/78%/23%/125/16 pages/5 pages (03b), 233/1,285/825/95.9/31.9/93.9/~80% (04c and `evals/results/2026-11-08_04c-v1.md`), QA 1.20/48%/95.9/92.3/80%/4.8 s/kappa 0.82 (05b), 8 of 9 and 9 of 9 (05d), the full 07b table, 0.598, +0.000 (07c), 487 and 87 (01), 16.3% and 15x (06a), 21 of 26 (02b), the agent budgets (05d and `config.py`), 1.7 and 32 to 39 per second (01). Problems 2 to 5 cover the exceptions. |
| Mermaid diagrams match the code | pass, small gaps | Stream keys, `lake:archive`, `lake:notifications`, the retrain DAG task names, the seven Helm deployments, the three services `values-offline.yaml` turns off, 5 MCP tools and the agent's Neo4j briefing write all match the code. Gaps in problem 8. |
| The quickstart can't contact Wikimedia | pass | `child_env()` overrides all five settings whose default is a Wikimedia URL, and a test derives that list from `Settings.model_fields`, so a new setting can't slip through. No `wikipulse/` module hard-codes a Wikimedia request URL (`WIKIPEDIA_PAGE` and `WIKIPEDIA_URL` only build citation links). The consumer and the enricher never start. The parent process only talks to Docker, Postgres, Redis, the local API and the Hugging Face cache (`local_files_only`). |
| The demo sample has no personal data and is small | pass | Every one of the 58,882 edit lines has exactly the keys `t, p, u, r`, with users as integers and no comments. The 134 page lines are Wikipedia page content (lead summary, links), not edit summaries. No e-mail addresses in either file. 800,288 + 399,528 bytes = 1.2 MB. |
| The docstring cleanup changed no behaviour | pass | AST comparison with docstrings stripped, main against HEAD, over all 74 changed `wikipulse/` files: the only difference is `agent/store.py`, where `BriefingSink` and `MemoryBriefingStore` were removed. `git grep` on `main` finds neither class used anywhere. |
| Moving the command reference didn't weaken the Claude Desktop config test | pass | The only change in `tests/mcp_server/test_client_config.py` is `README` now pointing to `docs/usage.md`. The assertions are the same: exactly one `mcpServers` JSON block, `_check_config`. `docs/usage.md` keeps every section heading the old README had and adds stages 6b to 9. |
| No personal e-mail anywhere | pass, but see problem 9 | No e-mail address in any added line, the sample or the GIF's strings. One added line contains the local Windows path with the user name. |

## Problems

1. **medium**, `scripts/quickstart.py` lines 343-352 (`wait_until_settled`): with `--graph`, `pending = graph and current.extracted < current.events` only counts `status = 'ok'` extractions. One failed extraction (`status = 'error'`, retried after 10 min of wall clock, then 20, 40 ...) keeps `same` at 0 forever. The script then never prints "Done" or the top events and never exits with `--exit-when-done`. This path has never run (learning note: "`--graph` is not verified end to end").
2. **medium**, `README.md` line 190, `docs/architecture.md` lines 221-223, `docs/blog/03-event-time-and-replayable-streaming.md` title and lines 29-31: "a replayed day gives the same spikes and events as the live run" isn't backed by the cited evidence. `determinism_check.py` compares two replays of one hour (02a), and the 03b check compares a replay with a restarted replay. No note compares a replay with a live run.
3. **low**, `README.md` lines 103-104, `docs/architecture.md` lines 229-231, `docs/blog/01-detection-with-imperfect-ground-truth.md` lines 3-4: they say every number, including the classifier's, is "a committed evaluation in `evals/results/`". There is no 07b, 07c, 09c or crash-test file there. Those numbers live only in the learning notes and MLflow.
4. **low**, `docs/blog/01-detection-with-imperfect-ground-truth.md` line 86: "raising it to 8 fails it the other way ... Both directions are kept as tests". The `min_edits=8` result is from a manual run (09c note line 402). The kept test, `test_a_stricter_threshold_fails_on_recall`, uses the strict variant (6, 4; 12, 5).
5. **low**, `docs/blog/03-event-time-and-replayable-streaming.md` line 29: "an hour of recorded edits replayed in two seconds". The note says each replay took about 2.5 minutes; only detection (offline, `compare_thresholds.py`) took about 2 seconds.
6. **low**, `docs/specs/10-polish.md` line 3: status is `done` while the learning note says CI on the final commit was not verified. CI is green on the final commit now except `smoke`, which was still running at review time.
7. **low**, `scripts/make_quickstart_sample.py` line 52: `SAMPLE_DIR = Path("demo") / "sample"` is relative to the working directory, unlike `quickstart.py`'s `ROOT`-based path, so running it from another directory writes the sample to the wrong place.
8. **low**, `docs/architecture.md` lines 129-145: the batch diagram leaves out that `ml.serve` reads spikes from Postgres and page revisions from Wikipedia, and that `extend_history`/`lake.backfill` calls EventStreams through the DAG. The pipeline diagram (line 20) says every arrow goes through a store, while QA to MCP to agent are direct calls.
9. **low**, `docs/learning/10-polish.md` line 117: the committed note contains the owner's local repository path and Windows user name. Nothing on `main` contained it before.
10. **low**, `scripts/quickstart.py` lines 236-243 (`Children.check`): a background child that exits with code 0 (for example `detect` or `api` ending early) is not noticed, and the script keeps polling a pipeline that has stopped.
11. **low**, `scripts/quickstart.py` lines 251, 269: `port_is_free`, `wait_for_api`, `Child`, `Progress.line` and `Children.stop_all` have no docstrings, although the cleanup's rule was a docstring on every public function. (The rule was applied to `wikipulse/` only.)

Verdict: fix

The default quickstart path is sound, offline by construction and tested, and the cleanup is behaviour-neutral. Before merging, fix problem 1 (or label `--graph` as unverified in the README), correct the replay-against-live claim (problem 2), and wait for `smoke` to finish.

## Questions for the human before merging

1. The quickstart runs the day at 300x with wall-clock poll intervals of 2 s. Why are the 74 spikes identical in every run while the event count moved between 69 and 70, and what would you change to make the event count deterministic as well?
2. `test_every_wikimedia_url_points_at_the_local_address` builds its list from the `Settings` defaults instead of hard-coding five names. What kind of future change does that catch, and what kind would it still miss (think of a URL that isn't a setting)?
3. With `--graph`, what happens to `wait_until_settled` when the LLM returns invalid JSON for one event, and how would you change the "settled" condition so it terminates without hiding real failures?

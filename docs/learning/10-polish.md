# Learning note: 10 Polish and presentation

## What was built

WikiPulse can now be tried by a stranger with one command and understood from one page.

| Piece | File | What it does |
|-------|------|--------------|
| Quickstart | `scripts/quickstart.py` | Starts Redis, Postgres and Neo4j with Compose, replays a bundled day through the real pipeline, opens the live page. Offline. |
| The sample | `demo/sample/edits.jsonl.gz`, `pages.jsonl.gz`, `README.md` | 58,882 anonymous human edits (4 Nov 12:00 to 6 Nov 00:00 UTC) and the 134 enriched pages the day's spikes need; 1.2 MB |
| Sample maker | `scripts/make_quickstart_sample.py` | Builds the sample once from the history lake and the `pages` table |
| Tests | `tests/test_quickstart.py` | 13 tests: sample edits become messages the consumer keeps, the recording replays in order, every Wikimedia URL setting is overridden, existing pages are kept, `--graph` settles despite a failed extraction, a service that ends is noticed, the sample is small and anonymous, every spike of the day has its page |
| README | `README.md` | Pitch, GIF, quickstart, diagram, stack, results, decisions, "more resources", how it was built with agents |
| Architecture | `docs/architecture.md` | Four Mermaid diagrams (pipeline, batch and ML, deployment, the quickstart) with a table of every step |
| Command reference | `docs/usage.md` | The old README's stage-by-stage commands, plus stages 6b, 6c, 7, 8c and 9 |
| Blog drafts | `docs/blog/01-...`, `02-...`, `03-...` | Detection with imperfect ground truth; a small local model as an agent; event time and replayable streaming |
| Cleanup | `wikipulse/**` | One-line docstrings on the 237 public functions that had none; two unused classes removed |

How the quickstart runs:

```
demo/sample/edits.jsonl.gz ──► data/quickstart/warmup.jsonl, day.jsonl  (raw recentchange format)
demo/sample/pages.jsonl.gz ──► Postgres pages (only titles not there yet)
warmup.jsonl ── stream.replay (max speed) ──► Redis wiki:quickstart:edits
detect.run (run quickstart) ──► spikes      events.run (--since the day) ──► events
api --run-id quickstart ◄── browser
day.jsonl ── stream.replay --speed 300 ──► the same stream, while the page watches
```

## Key concepts

### Developer experience: one command that a stranger can follow

What makes `scripts/quickstart.py` work on a clean machine:

- **Only Docker and uv are needed.** `uv run` installs Python and the packages; Compose
  pulls the images; no `.env` is required, because every setting has a default. Ollama is
  only needed for the optional `--graph`.
- **It runs the real code, not a demo path.** The sample is converted into a raw
  recording (`raw_message()`, `write_recording()`) and fed through `wikipulse.stream.replay`,
  the same `Consumer.process` as the live consumer, into a Redis Stream that the real
  detector, grouper and API read. Only the enricher is replaced, by loading its output
  (`load_pages()`).
- **Warm-up, then show.** The detector needs to know each page's normal rate. The 12
  hours before the day are replayed at full speed (about 25 s); then grouping and the
  page start and the day plays at 300x (5 minutes), so events visibly arrive.
- **Offline by construction, not by promise.** `child_env()` points every Wikimedia URL
  setting (`STREAM_URL`, `WIKI_API_URL`, `WIKI_REST_URL`, `WIKIDATA_API_URL`,
  `WIKIDATA_SPARQL_URL`) at `127.0.0.1:9`, where nothing listens, so even an accidental
  call fails at once. A test checks that every setting whose default is a Wikimedia URL
  is in that list, so a future setting can't slip through. If the embedding model is
  already cached, `HF_HUB_OFFLINE=1` keeps Hugging Face offline too.
- **Safe to repeat and safe next to real data.** Its own run id (`quickstart`) and stream
  (`wiki:quickstart:edits`); `--reset` for detector and grouper; pages already in the
  table are never replaced. Ports and the Compose project come from the usual settings.
- **Failure is explained.** Docker missing or not running, a busy port, a child process
  dying: each stops with one sentence and the path of the log (`data/quickstart/logs/`).
  `Children.check()` treats a *service* (detector, grouper, graph runner, API) that ends
  as a failure even with exit code 0; only *jobs* (`may_finish=True`: the replays,
  migrations) may end on their own. Otherwise a pipeline that had quietly stopped would
  go on printing unchanged progress lines.

### Communicating technical work to different audiences

- **A reviewer skimming for 30 seconds** gets the README's first screen: one sentence of
  what, a GIF that proves it runs, and one command.
- **An engineer** gets the diagram, the stack, and design decisions phrased as
  trade-offs ("event time costs X and buys Y").
- **Someone checking the claims** gets numbers with sample sizes and margins, each
  linked to `evals/results/` and the learning note, including the unflattering ones
  (0% lead-story recall for the default detector, 48% fully correct answers) and why.
- **A reader** gets blog drafts that tell one lesson each, with the project as evidence.

## Design decisions and deviations

- **A new bundled sample, not the gate fixture.** The CI gate's 16-hour recording
  (6/7 November) has only 28 spikes and half of their pages were never enriched. The
  sample is 5 November, the day all the evaluations, notes and the graph snapshot talk
  about (Ueno Bank, the US elections), built from the history lake in the gate's compact,
  anonymous format (users numbered, no edit summaries), 1.2 MB in total.
- **Neo4j is part of the "minimal system".** The first fresh-clone run, without it,
  failed: the API's health check waits on the graph driver's connection timeout, and
  every poll reads the graph. An empty Neo4j costs about a minute of start-up and makes
  the page healthy; `--graph` fills it.
- **`--graph` is an addition.** The spec's acceptance mentions Ollama, but extraction
  calls Wikimedia (burst text, Wikidata). With `--graph` the graph runner talks to
  `scripts/fake_wikimedia.py` (spec 09b's offline stand-in) and reads the page summaries
  from Postgres; entities stay unlinked. The default path needs no LLM.
- **`DETECT_SNAPSHOT_INTERVAL_S=2` for the quickstart.** The detector hands spikes over at
  each snapshot (30 s by default), which at 300x is 2.5 hours of the day at once; the page
  jumped. It is a wall-clock setting: it changes when spikes appear, not which.
- **The command reference moved to `docs/usage.md`**, so the README can be read by a
  stranger; the test of the Claude Desktop snippet follows it there.
- **GIF made, not left as a placeholder.** Headless Chrome, driven over the DevTools
  protocol by a throwaway script (not committed), opened the page once during the final
  fresh-clone run, set the pages filter to "1+", and took a screenshot every 3 s; a
  marker variable in the page survived to the end, so it was never reloaded. Frames were
  joined with Pillow (77 distinct frames, 2.2 MB). A narrated demo video is still left to
  the human.
- **Cleanup kept conservative.** No TODOs were found. Docstrings were added only to
  public module-level functions (237); public methods and classes without one (about 330)
  were left, as the spec names functions. The only code removed: `BriefingSink` and
  `MemoryBriefingStore` in `agent/store.py`, referenced nowhere.
- **CI on the final commit is not verified here**: the branch is not pushed. Locally the
  same checks pass (below).
- **Review fixes (docs/reviews/10-polish.md).** The README, `architecture.md` and blog 03
  (title included) no longer claim a replay equals the live run: the evidence is replay
  against replay (`determinism_check.py`, 02a) and replay against a restarted replay
  (03b); no live run was compared with a replay of its own recording. Statements that
  "every number is in `evals/results/`" now say the classifier, gate and crash-test
  numbers live in the learning notes (and MLflow). Blog 01 says the `min_edits=8`
  result is a manual run and names the kept tests; blog 03 gives the replay's real
  duration (about 2.5 minutes; 2 seconds was offline detection). The batch diagram now
  shows `ml.serve` reading spikes from Postgres and revisions from the Wikipedia API, and
  `extend_history` calling the backfill; the pipeline text says the answering side
  (API, MCP, agent) uses direct calls. `make_quickstart_sample.py` writes under the
  repository root from any working directory. The quickstart's helpers got docstrings.

## Results

### The fresh-clone test

The user's Compose stack (project `wikipulse`) was running and was left alone. Each test
used a clone in the scratchpad with its own Compose project and ports:

```
git clone -b spec-10-polish <path of your clone> <scratchpad>/fresh
cd <scratchpad>/fresh ; uv sync
set COMPOSE_PROJECT_NAME=wpquickstart, REDIS_PORT=16379, POSTGRES_PORT=15432,
    NEO4J_HTTP_PORT=17474, NEO4J_BOLT_PORT=17687, API_PORT=8010 and the matching
    REDIS_URL, POSTGRES_DSN, NEO4J_URI (no .env)
uv run python scripts/quickstart.py --no-browser --exit-when-done
```

| Run | Result |
|-----|--------|
| 1 (Redis and Postgres only) | failed: the API never answered `/health` within 120 s (no Neo4j); fixed by starting Neo4j |
| 2 | passed: 74 spikes, 70 events on 5 November |
| 3 (fresh clone, fresh volumes) | passed in 7 min 5 s: 74 spikes, 69 events |
| 4 (final commit, fresh clone, fresh volumes) | passed in 6 min 57 s: 74 spikes, 69 events (5 with 2+ pages); the page captured for the GIF |

Between runs, `docker compose -p wpquickstart down -v` removed only the test project's
containers and volumes; the user's seven containers stayed up throughout. After run 4,
Docker Desktop stopped (not by this session), so that cleanup waited until the `--graph`
run below, which ended with `docker compose -p wpquickstart down -v`.

**`--graph`, end to end (after the review).** The first version never settled if one
extraction failed: it waited until the count of *successful* extractions reached the
number of events, but a failed one is retried only after 10 minutes of wall clock, then
20, 40, ... `wait_until_settled()` now asks the graph runner's own rule
(`wikipulse.db.graph.pending_events`) which events still need extraction, with the
retry delay set to a year (`NO_RETRY`): an event counts as finished once its current
form is extracted **or** has failed. Failures are counted in the progress line and
listed at the end with their error (`report_failed_extractions()`), so they are reported,
not hidden. Then a fresh clone at the fix (own Compose project `wpquickstart`, the ports
above, fresh volumes, the host's Ollama with `qwen3:4b`, the fake Wikimedia):

```
uv run python scripts/quickstart.py --graph --no-browser --exit-when-done
```

passed in 10 min 26 s: 74 spikes, 69 events (5 with 2+ pages), **69 of 69 in the
graph, 0 failed extractions**; it printed the top events and exited 0. Extraction
ran alongside the replay and finished 3 minutes after it. Afterwards
`docker compose -p wpquickstart down -v` removed the test project; the user's
`wikipulse` stack stayed up throughout. (The failure path itself is covered by the unit
test `test_a_failed_extraction_does_not_keep_the_graph_run_from_settling`; this run had
no failure to exercise it.)

- **Spikes are exactly reproducible**: 74 in every run, the same 74 the detector raises
  offline on the same edits (`gate.detect`, checked by
  `test_every_spike_of_the_shown_day_has_its_page`).
- **Events differ by one between runs (70 vs. 69).** Live grouping polls by wall clock;
  at 300x the order in which spikes become visible relative to events closing can vary
  (in run 3, *Rise and Fall (Indian TV series)* gained a second page). The 03b
  determinism holds for replays with `--once`; a live run at extreme speed is not one.
- **Nothing contacted Wikimedia**: all Wikimedia URLs pointed at a dead local port; the
  enricher and consumer never ran.

### Checks

- After the review fixes: `uv run pytest` 1,299 passed, 1 skipped.
- Before them: `uv run pytest`: 1,294 passed, 1 skipped after fixing the one failure the README move
  caused (`test_the_readme_config_snippet_is_valid_json`, now reading `docs/usage.md`).
- `uv run ruff check .` and `uv run ruff format --check .`: clean.

## Questions to test your understanding

1. The quickstart sets every Wikimedia URL to `http://127.0.0.1:9` instead of simply not
   starting the processes that call Wikimedia. What does that add?
2. Why does the quickstart replay the 12 hours before the day at full speed instead of
   starting with the day?
3. The spikes are identical in every run but the events once differed by one. Which
   design rule explains the first, and what explains the second?

<details>
<summary>Answers</summary>

1. Defense in depth. Not starting the consumer and enricher prevents the known callers;
   overriding the URLs also stops unknown ones, such as a later change that makes the
   grouper or API fetch something. A call to a dead loopback port fails immediately and
   visibly instead of quietly reaching Wikimedia, and the test that lists every
   Wikimedia URL setting keeps the override complete.
2. The detector compares each page's edits with its normal rate, learned from history
   (a decaying baseline). A detector that starts cold treats every busy page as unusual
   (the 02b warm-up raised 44 spikes in half a day for this reason). Full speed costs
   about 25 seconds and nothing is shown for it: grouping only takes spikes from the day.
3. Event time: the detector never reads the clock, so the same edits give the same spikes
   however fast or in whatever batches they arrive. Grouping in live mode reads spikes as
   they are written and closes events as its watermark advances, every two seconds of
   wall clock; at 300x the interleaving of "new spike visible" and "event closed" can
   differ between runs. Replaying spikes with `events.run --once` removes that.

</details>

# Learning note: Spec 03b, Event grouping

## What was built

Spikes are about pages; news is about stories. This spec groups the spikes that belong to
the same story into one **event**: on the day after the 2026 US midterms, the governor
races, the Senate races and the newly elected representatives became one event instead
of sixteen loose pages.

| File | Role |
|------|------|
| `wikipulse/events/similarity.py` | `pair_score(a, b, config)`: one score in [0, 1] for two spiking pages, the weighted average of three signals (links, start times, summary embeddings). `SpikePage` is a spike plus what grouping needs from its page. |
| `wikipulse/events/embed.py` | `Embedder`: turns page text (`page_text()`: title, short description, summary) into unit vectors with the local model `BAAI/bge-small-en-v1.5` (sentence-transformers), and remembers every result. |
| `wikipulse/events/grouper.py` | `Grouper.add(spike)`: the online algorithm. Joins the spike to an active event, starts a new one, or merges events the spike connects. `advance(now)` ends events whose spikes are all over. Pure logic: no database, no clock. |
| `wikipulse/events/run.py` | `uv run python -m wikipulse.events.run`: `GroupingRunner` reads spikes and their enriched pages from Postgres in order, feeds the grouper and stores the events. Live (polls every 10 s) or `--once` (replay a time range). Rebuilds its state from Postgres after a restart. |
| `wikipulse/events/feed.py` | `uv run python -m wikipulse.events.feed`: the active (or, with `--all`, all) events of a grouping run with their pages, biggest first, plus a summary line (events, pages per event). |
| `wikipulse/db/migrations/0004_create_events.sql`, `wikipulse/db/events.py` | Tables `events` and `event_pages`; the queries for unassigned spikes, active events, spike end times, the feed and the evaluation. |
| `wikipulse/eval/grouping_labels.py` | The append-only pair labels file `evals/labels/grouping_labels.jsonl`, the LLM pair labeler (`OllamaPairLabeler`) and the manual labeling loop. |
| `wikipulse/eval/grouping.py` | `uv run python -m wikipulse.eval.grouping label|report`: stratified pair sampling, pairwise precision and recall, the report in `evals/results/`. |
| `wikipulse/config.py` | `EventsConfig` and the `EVENTS_*` settings (all in `.env.example`). |
| `tests/events/`, `tests/eval/test_grouping.py` | 40 unit tests (hand-made spikes and vectors, a fake embedding model, a mocked Ollama) and 1 integration test (real Postgres). |

How the pieces fit:

```
Postgres `spikes` (detector) ──┐
Postgres `pages`  (enricher) ──┤  every 10 s (live) or once (replay):
                               ▼
                  GroupingRunner.step()
                    1. refresh end times of spikes in active events
                    2. unassigned spikes, oldest first (live: wait up to 120 s
                       for a spike's page to be enriched)
                    3. Embedder: page text ──► 384-number unit vector
                    4. Grouper.add(spike) for each, in order
                         close events that are over ─► score vs. each active event
                         ─► new event / join / merge + join
                    5. Grouper.advance(detector watermark)
                    6. save changed events ──► Postgres `events`, `event_pages`
                               │
                               ▼
               events.feed (people)      eval.grouping (numbers)
```

## Key concepts

### Clustering with several signals

Clustering means putting similar things in the same group without being told the groups
in advance. Here a "thing" is a spike, and similarity is `pair_score()` in
`similarity.py`, built from three signals, each between 0 and 1:

- **link**: one page links to the other (`link_signal()`). Wikipedia editors link related
  articles, so this is strong evidence. One catch: list pages like *Deaths in 2026* link
  to thousands of articles, so a link *from* such a hub says little. A link from a page
  with more than `EVENTS_LINK_HUB_SIZE` (1000) links counts `1000 / links`
  (`link_strength()`).
- **time**: the spikes started close together (`time_signal()`): 1 for the same moment,
  falling linearly to 0 at `EVENTS_TIME_WINDOW_H` (6) hours apart.
- **embed**: the summaries mean similar things (`embed_signal()`), from **cosine
  similarity** of embeddings.

The total is a weighted average, `(0.4 link + 0.2 time + 0.4 embed) / 1.0`. Two spikes of
the same page always score 1. With the defaults, a link plus a simultaneous start
(0.4 + 0.2 = 0.6) is enough to join (threshold 0.55), while time alone (0.2) never is.

**Cosine similarity** measures the angle between two vectors: 1 when they point the same
way, 0 when they are at right angles. For vectors of length 1 it is just the dot product
(`cosine()` is `np.dot(a, b)`). The embedding model is trained so that texts with similar
meaning get vectors with a high cosine.

### Embeddings as a general tool

In RAG (stage 5) embeddings find documents for a question. Here the same idea is used for
clustering: `Embedder.embed_many()` turns each page's text (`page_text()`:
"Larry (cat). Chief Mouser. Larry is a cat ...") into 384 numbers with
`BAAI/bge-small-en-v1.5`. It runs on the CPU: loading takes about 5 s (42 s the first
time, when it is downloaded from Hugging Face), and 665 summaries take about 10 s.

One practical lesson: raw cosine values are not "probabilities". Between the summaries of
two random spikes of 5 November the median cosine was **0.44**, the 90th percentile 0.53,
the 99th 0.64. Unrelated texts are far from 0. That is why `embed_signal()` rescales:
a cosine at or below `EVENTS_EMBED_FLOOR` (0.5) counts 0, 1.0 counts 1. Without that,
every pair would get a free 0.4 x 0.44 = 0.18 from the embedding signal.

### Connected components and online grouping

Think of a graph: every spike is a node, and two spikes are joined by an edge when their
pair score reaches the threshold. A **connected component** is a set of nodes you can
walk between along edges. If A is like B and B is like C, then A, B and C are one
component even if A and C have nothing in common.

The grouper builds these components **online**, one spike at a time, as spikes arrive
(`Grouper.add()`), instead of clustering a finished dataset:

1. Close events that are over at the new spike's start time (`advance()`).
2. Score the spike against each active event. An event's score is its *best* page
   (`best_score()`: "single linkage", one edge is enough).
3. No event reaches the threshold: start a new event. One does: join it. Several do:
   the spike is an edge between them, so they are one component: **merge** them into
   the oldest (lowest number), mark the others `merged` with `merged_into`, then join.

Online has consequences an offline clustering doesn't: a decision is made when the spike
arrives and never revised (only merged forward), and it can only use what is known then.
The one merge on 5 November happened this way.

Single linkage has a known weakness: **chaining**. One page with edges to two different
stories glues them together. Hubs such as *Deaths in 2026* are the classic case (two
different deaths, each linked from the list), which is what the hub rule damps.

### When an event ends

Requirement 2: "An event ends when all its spikes have ended." The detector ends a spike
30 minutes (its quiet period) after its last hot moment and stores `ended_at` = the last
edit. In a replay all end times are already in Postgres, so the grouper must not "see"
an end before the live system would have. `Event.is_over()` therefore counts a spike as
over at `ended_at + EVENTS_SPIKE_CLOSE_MIN` (30 min), the same moment in a live run and
in a replay. Once ended, an event takes no new spikes: a page that spikes again later
starts a new event.

### Determinism and event time

Requirement 1: same input order, same events. Everything in `Grouper` uses event time
(spike timestamps, the detector's watermark), never `datetime.now()`. Spikes are read in
the order `(started_at, title, id)`. Cosines are rounded to 4 decimals
(`COSINE_DECIMALS`), so the last bits of floating-point noise (for example from batching
the embedding model) can't flip a decision at the threshold. Live, a spike whose page is
not enriched yet holds back the spikes after it (`ready_rows()`), so they keep their
order.

### Evaluating clustering: pairwise precision and recall

Clustering is notoriously hard to evaluate:
- **There is no single right answer.** Is "the 2026 US elections" one story, or is each
  Senate race its own? Both are defensible; the answer is a choice of *granularity*.
- **Groups can't be matched one to one.** If the truth has one group and the system made
  two, which of the two is "the" match?
- **Most pairs are trivially different**, so naive sampling sees almost no positives.

**Pairwise metrics** sidestep the matching problem: look at pairs of spikes instead of
groups. A pair is either put together by the grouper or not, and either the same story
or not. Precision = of the pairs put together, the share that is the same story. Recall =
of the same-story pairs, the share put together.

665 spikes make 220,780 pairs, so the evaluation samples. A plain random sample would be
almost all "different". `stratify()` in `eval/grouping.py` splits the pairs into three
**strata** and samples each one separately (`sample_pairs()`):

| Stratum | Which pairs | Why |
|---|---|---|
| `together` | put in one event by the run | precision comes straight from it |
| `near` | not together, but linked or with summaries at cosine >= 0.65 | where misses hide |
| `far` | everything else | almost certainly different; checked with a small sample |

Each labeled pair then stands for `population / labeled` pairs of its stratum
(**stratified estimation**, `StratumCount.estimated_same`). Recall is
`found / (found + missed)` with these estimated counts (`GroupingMetrics.recall()`). The
report also shows the unweighted recall over just the labeled pairs, which is badly
biased (it treats one labeled `together` pair, standing for 7 pairs, the same as one `far`
pair, standing for 8,700). The far stratum shows the limit of sampling: 0 same-story
pairs in 25 labeled can still mean up to about 26,000 in the 218,000 (the "rule of
three": with 0 hits in n, the 95% upper bound of the rate is 3/n).

Samples are drawn in a fixed hash order (`sample_order()`), not with a random generator:
a pair always gets the same rank, so another grouping run over the same spikes samples
mostly the same pairs and reuses their labels.

## Design decisions and deviations

- **Dependencies.** `sentence-transformers` (allowed by the spec) and `torch` listed
  explicitly so that it comes from PyTorch's CPU-only package index
  (`[tool.uv.sources]` in `pyproject.toml`). On Linux, the PyPI build of torch pulls in
  several GB of CUDA libraries, which CI doesn't need. AGENTS.md lists PyTorch for a later
  stage; here it is only the engine under sentence-transformers.
- **Signals.** Link = either direction, matched on the spike title and the canonical
  title, with the hub rule (not in the spec). Time = distance between spike *starts*
  (the spec says "started close in time"). Embedding = cosine rescaled above a floor (not
  in the spec; see "Embeddings"). Weights are normalized by their sum, so only their
  ratios matter.
- **Pages that aren't enriched**: their links are empty and their embedding uses the
  title only. A replay groups everything it finds; live, a spike waits up to
  `EVENTS_ENRICH_WAIT_S` (120 s) for its page (03a enriches within a minute).
- **Single linkage and merging into the oldest event**: the simplest rule that makes the
  events connected components, and the event number that people may already have seen
  survives.
- **Grouping runs.** `events.run_id` names a grouping run, `spike_run_id` the detector run
  it reads (default: the same name), so several weight settings can be compared over the
  same spikes. Events are numbered per run (1, 2, 3, ...); a merged event keeps its row
  (`status = 'merged'`, `merged_into`), and its pages move to the survivor.
- **State after a restart** comes from Postgres, not Redis: the active events and their
  spikes are read back (`active_members()`), their pages re-embedded, numbering
  continues, and unassigned spikes are simply the ones without an `event_pages` row.
- **Replay range.** With `--until`, the grouper's clock stops at `--until`, as if the
  data ended there; events still running at midnight stay active, and a later run with
  `--since` the next day continues them.
- **Title**: the canonical title of the page with the highest *spike score*
  (requirement 3); ties go to the earlier spike.
- **Labels are pairs.** The spec asks for "labeled groupings" and an LLM that judges
  "sampled page pairs". A labeled grouping of n pages is the same as its n(n-1)/2 pair
  labels, so the file stores pairs only (`same` / `different` / `unsure`, with
  `labeler`, `model` and `prompt_version`). As with the spike labels, the file is
  append-only and a human label beats an LLM label.
- **Labeling without the network.** The labeler sees both pages' titles, descriptions,
  summaries and spike times from the `pages` table, so labeling makes no Wikimedia
  requests.
- **One prompt revision.** With the first prompt, which mentioned in passing that "a set
  of elections held together counts as one story", qwen3:4b called most pairs of US
  midterm pages "different" (*Senate election in Maine* / *Senate election in New
  Hampshire*: "different states, separate news stories"). Version 2 states the
  granularity rule first, with examples (`PROMPT_VERSION = 2` in
  `grouping_labels.py`). Both versions' labels are in the file; the newest counts.
  This is the "no single right answer" problem in practice: the measured precision moved
  from 44% to 78% by changing the *definition*, not the grouper.
- **The sample is the 4b model's, not the 8b's**: like 02b, only `qwen3:4b` is
  installed (`--model ollama:qwen3:4b`).
- **Day**: 2026-11-05, spikes of the detector run `02b-loose` (665 spikes, all pages
  enriched by 03a). The default detector run has only about 70 spikes a day, too few to
  group; the loose run is the one stage 2 found richest in news.

## Results

All on 2026-11-07, over the spikes of run `02b-loose` that started on 2026-11-05 (UTC).

**The replay** (acceptance criterion 2):

```
uv run python -m wikipulse.events.run --spike-run 02b-loose --run-id 03b-default --since 2026-11-05 --until 2026-11-06 --once --reset
uv run python -m wikipulse.events.feed --run-id 03b-default --all
```

took 31 s (about 5 s loading the model, 10 s embedding). 665 spikes became **562
events** (563 created, 1 merged away; 17 still active at midnight). Pages per event:
**mean 1.18, median 1, max 16**.

| Pages per event | 1 | 2 | 3-5 | 6-10 | 11+ |
|---|---:|---:|---:|---:|---:|
| Events | 515 | 29 | 15 | 1 | 2 |

The biggest events (from the feed):

```
#395 2026 United States gubernatorial elections  [16 page(s), 11-05 18:44 to 11-05 22:22, ended]
#2 List of new members of the 120th United States Congress  [16 page(s), 11-05 00:05 to 11-05 03:34, ended]
#333 Ueno Bank  [9 page(s), 11-05 16:49 to 11-05 20:15, ended]
      Ueno Bank, Pedro Alliana, Santiago Peña, 2026 Paraguay banking crisis, Vice President of Paraguay, ...
#183 Ramayana: Part 1  [5 page(s)]   the film, four spikes, plus its VFX studio DNEG
#288 Herbert Blomstedt  [4 page(s)]  Blomstedt, Deaths in 2026, and two other deaths (chaining)
#309 2026–27 Indian Super League  [4 page(s)]  with the SAFF Championship and Bangladesh's team
```

Most events have one page: on a normal day most spikes are unrelated (a school, a TV
episode, an edit war). The two US election events are the midterm aftermath, split in two
because the first one ended in the early morning (all its spikes were over) before the
evening wave of results started.

**Determinism** (requirement 1): the same replay under another run id, and a replay
stopped at 12:00 and restarted (state rebuilt from Postgres, 11 active events restored),
both gave the same 665 assignments, joined scores and 563 event rows (compared in SQL:
0 differences).

**Live mode**: `events.run --spike-run 03a-latency` (15 spikes of the 03a live test)
polled, grouped them into 14 events (the two *Beach wrestling at the 2026 Summer Youth
Olympics* pages together), and kept polling until stopped.

**Evaluation** (`uv run python -m wikipulse.eval.grouping label|report --run-id <run>
--day 2026-11-05`), 125 sampled pairs per run (50 together, 50 near, 25 far), LLM labels
from `ollama:qwen3:4b` with prompt v2, about 1.5 s per label. Reports:
`evals/results/2026-11-07_03b-default.md` and `..._03b-thr050.md`.

| Run | Change | Events | Max pages | Pairs together | Precision (95%) | Recall (estimated) |
|---|---|---:|---:|---:|---:|---:|
| `03b-default` | defaults | 562 | 16 | 369 | **78%** ± 11.5 pp | **24%** |
| `03b-thr050` | `EVENTS_JOIN_THRESHOLD=0.5` (the one tuning step) | 561 | 17 | 385 | 78% ± 11.5 pp | 24% |
| `03b-close180` | diagnostic: `EVENTS_SPIKE_CLOSE_MIN=180` | 481 | 36 | 1,347 | 66% ± 13.1 pp | 64% |

Default run, stratum by stratum: `together` 369 pairs, 39 of 50 labeled same; `near` 2,111
pairs, 22 of 50 same (an estimated 929 missed same-story pairs); `far` 218,300 pairs, 0 of
24 same (1 unsure). With the first prompt (v1) the same run measured 44% precision and
24% recall; see "One prompt revision".

What the numbers say:

- **Precision is decent (78%), recall is low (24%).** The grouper is cautious: when it
  puts pages together they are mostly one story, but it leaves most of a big story's
  pages in separate events.
- **The tuning step taught more than it gained.** Before running it, I scored every
  labeled pair directly with candidate settings (pair score >= threshold, weighted by
  stratum). Lowering the threshold to 0.5 looked best there (pair-level precision 90%,
  recall 57%, against 88% and 49% for the defaults), while more embedding weight or a
  higher threshold lost recall. Run online, the change did almost nothing: one page
  more in an event, the same sample (so no new labels), the same metrics. The pair-level
  check ignored that a spike can only join an *active* event.
- **Event lifetime is the real limit.** Most missed same-story pairs had spikes hours
  apart (*Lloyd Geering* / *Deaths in 2026*, US races in the morning and in the evening,
  the same page spiking twice). By then the first event has ended, so no score can
  join them (requirement 2). The diagnostic run that keeps events open 3 hours after
  their last edit raised recall to 64% and lowered precision to 66% (the intervals of
  the two precisions overlap): bigger events, including a 36-page US election event,
  and more chaining: a 21-page "event" of eight different people (mostly recent deaths),
  glued together by six spikes of *Deaths in 2026* (the hub rule weakens such links, but with time and
  summary similarity they still pass). The defaults stay with the spec's rule; whether events should live
  longer is a product decision for the human (and maybe for stage 4's titles).
- **The far stratum can't be measured well.** 0 of 24 is good news, but the rule of three
  still allows up to about 27,000 same-story pairs there. Recall is therefore stated
  over all pairs *assuming* the far stratum is (nearly) empty, which looking at its pairs
  (*Eevee* / *Kimmarie Johnson*) makes plausible.
- **Typical errors.** False merges: two different historical figures linked to each other
  (*Jean Talon* / *Samuel de Champlain*), two deaths joined through *Deaths in 2026*
  (*Herbert Blomstedt* / *Ali-Asghar Fani*), and pairs the LLM still split by office
  (*gubernatorial* / *judicial elections*). Misses: deaths and the deaths list hours
  apart, Premier League season pages, US election pages in different events.
- **LLM labeler quality.** Some labels are wrong (*2026 Players Championship Finals*, darts,
  and *2026 Rolex Paris Masters*, tennis, labeled "same"), so these numbers carry the
  4b model's errors too. `--manual` on the same sample would measure how often you agree.

**Tests:** 41 new tests (40 unit, 1 integration). `uv run pytest`: 263 passed;
`uv run pytest -m integration`: 20 passed. `ruff check` and `ruff format --check` are
clean.

## Check your understanding

1. Spikes A and B are not similar, but spike C, arriving later, scores above the
   threshold with both. What does the grouper do, and which event number survives?
   What is the risk of this rule?
2. The labeled sample of the default run contains 39 same-story pairs among 50
   `together` pairs and 22 among 50 `near` pairs, and the strata hold 369 and 2,111
   pairs. Estimate the recall over these two strata. Why is "39 / (39 + 22) = 64%"
   wrong?
3. Why does `Event.is_over()` add 30 minutes to a spike's `ended_at`, and what would a
   replay get wrong without it?

<details>
<summary>Answers</summary>

1. C is an edge between the two components, so the events of A and B are merged into
   the older one (the lower number), which keeps its number; the other is marked
   `merged` with `merged_into` pointing at the survivor, and C joins. The risk is
   chaining: one page related to two different stories (a hub like *Deaths in 2026*)
   glues them into one event, and with single linkage each new link can grow it
   further.
2. Found: 369 x 39/50 = 288 same-story pairs put together. Missed: 2,111 x 22/50 = 929.
   Recall is about 288 / (288 + 929) = 24%. The unweighted 64% treats each labeled pair
   as equally important, but each labeled `near` pair stands for about 42 real pairs
   and each `together` pair for only about 7: the `together` stratum was sampled six
   times more densely, so it dominates the unweighted count.
3. The detector only *declares* a spike ended 30 minutes after its last hot moment;
   until then a live grouper sees it as active. A replay reads `ended_at` (the last
   edit) from a finished table; without the 30 minutes it would close events up to half
   an hour earlier than the live run, and spikes in that half hour would start new
   events in the replay but join in the live run.

</details>

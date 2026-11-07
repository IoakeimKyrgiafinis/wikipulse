# Learning note: Spec 02b, Detection evaluation

## What was built

A way to put numbers on the spike detector: how much real news it finds (recall), how
much of what it finds is news (precision), and how fast. Ground truth is Wikipedia's
own "Current events" portal, plus a labeled sample of spikes.

| File | Role |
|------|------|
| `wikipulse/eval/wiki_client.py` | `WikiClient`: a polite, caching HTTP client for Wikimedia APIs (User-Agent, one request at a time with a pause, retries with backoff, JSON file cache). |
| `wikipulse/eval/ground_truth.py` | Fetches a day's portal page (`action=parse`, `prop=links\|wikitext`), extracts all article links and the *lead* link of each entry, resolves redirects (`action=query&redirects=1`). Cached in `data/ground_truth/`. |
| `wikipulse/eval/metrics.py` | The metrics as pure functions: `recall_for_day`, `pooled_recall`, `match_spikes`, `portal_precision`, `time_to_detection`, `label_precision`. |
| `wikipulse/eval/label_store.py` | The append-only labels file `evals/labels/spike_labels.jsonl` (`LabelRecord`). |
| `wikipulse/eval/label.py` | `uv run python -m wikipulse.eval.label`: samples 50 spikes, fetches their context (page summary, edit comments), and has a local LLM (or you, `--manual`) label them. |
| `wikipulse/eval/detection.py` | `uv run python -m wikipulse.eval.detection`: builds the report (`evals/results/<date>_<name>.md` and `.json`); `--compare A B` prints the differences. |
| `scripts/detect_recordings.py` | Runs several detector variants over days of recordings in about a minute and stores their spikes in Postgres (see "Deviations"). |
| `tests/eval/` | 48 unit tests (saved API responses in `tests/fixtures/eval/`, hand-made spikes) and 1 integration test (real Postgres). |

How the pieces fit:

```
recordings (data/recordings/*.jsonl)
   │  scripts/detect_recordings.py  (or stream.replay + detect.run)
   ▼
Postgres `spikes` (one run_id per detector variant)
   │                                   ┌── Portal:Current events/<day>  (MediaWiki API, cached)
   ├── eval.label ── sample 50 ──► LLM │
   │      │  page summary + edit       │
   │      ▼  comments (cached)         │
   │   evals/labels/spike_labels.jsonl │
   ▼                                   ▼
eval.detection ── metrics.py ──► evals/results/<date>_<name>.md / .json
                                       │
                         eval.detection --compare A B
```

## Key concepts

### Evaluation design with imperfect ground truth

There is no list of "all the news that happened". The portal is the best free
approximation, but it is imperfect in two different ways, and each one bends a
different metric:

- **The portal is incomplete.** It lists maybe 20 to 40 stories a day, chosen by
  editors, heavy on international politics, conflicts and disasters. A football
  match, a TV finale or the death of a minor actor usually never appears. So a spike
  that is *not* on the portal may still be real news. That is why
  `portal_precision()` is only a **lower bound**: the true precision is at least this
  high, probably much higher.
- **The portal over-links.** An entry like "Saudi Arabia launches an airstrike against
  a market in Taiz, Yemen" links *Saudi Arabia*, *Airstrike*, *Taiz* and *Yemen*.
  Nobody expects a burst of editing on *Airstrike*. So recall over *all* links
  (`recall_all`) is pessimistic; recall over **lead links** (the first link of each
  entry, `lead_links()` in `ground_truth.py`) is closer to "did we catch the story".
  Lead links are imperfect too: on 6 November the leads included *Australia* and
  *United States* (the first word of a sentence is often the actor country).

The precision lower bound is filled in by **labels**: a random sample of spikes,
each judged "news / not news / unsure". That gives an *unbiased* estimate of
precision (the sample is random), but with two caveats the report states:

- 50 labels give a wide margin of error. With 50 labels and a true precision of 50%,
  the 95% interval is roughly ±14 percentage points (`1.96 * sqrt(0.5 * 0.5 / 50)`).
- An LLM labeler is itself a model with errors. Its labels are an **estimate**; a
  human label always wins (`metrics.pick_label()`), and `--manual` lets you measure how
  often you and the LLM agree.

Matching rules, all in `metrics.py`:

- A portal date D is the UTC day [D 00:00, D+1 00:00). A spike is its active period
  [`first_edit_at`, `ended_at`]. `gap_hours()` is 0 if they overlap, otherwise the
  hours between the nearer ends.
- Recall: a portal article counts as found if some spike of it has a gap of at most
  24 hours (`RECALL_WINDOW_H`): any time from the day before to the day after.
- Precision: a spike counts as on the portal if any portal day within 48 hours
  (`PRECISION_WINDOW_H`) links its article.
- Titles are compared after redirect resolution: the portal linked *Tampa*, the
  article (and its edits) are *Tampa, Florida*.

### Precision, recall and their trade-off

- **Precision**: of the spikes we raised, how many were news? (`news / spikes`)
- **Recall**: of the news, how many did we raise a spike for? (`found / portal articles`)

A stricter detector (more edits and editors needed to start a spike) raises fewer,
more certain alarms: precision goes up, recall goes down. A looser one catches more
stories but also more edit wars and busy wikiprojects. The results below run four
variants on the same two days to show it.

### Time to detection

`detection_delay_minutes()` is `started_at - first_edit_at`: from the first edit of
the burst (the oldest edit in the window that triggered the start rule) to the
moment the rule held. It is measured on spikes that match the portal, as the spec
asks. It says how long the detector waits to be sure, not how long after the
real-world event Wikipedia reacted: that second delay is outside what we can see.

### Working with the MediaWiki API politely

`WikiClient` (in `wiki_client.py`) does everything AGENTS.md asks:

- the User-Agent from `USER_AGENT` on every request,
- one request at a time and at least `WIKI_API_MIN_INTERVAL_S` (1 s) between two,
- retries on 429/5xx with exponential backoff, honouring `Retry-After`,
- every response cached as a JSON file with its fetch time. Old portal days never
  change, so a cached copy fetched 3 or more days after the day is used forever;
  younger copies are refreshed after 6 hours (`freshness_rule()`).

Batching matters too: redirects for 107 links take 3 requests (50 titles per
`action=query`), not 107. A second evaluation of the same days makes **zero**
requests.

Try it (PowerShell):

```
curl.exe -A "WikiPulse-learning (you@example.com)" "https://en.wikipedia.org/w/api.php?action=parse&page=Portal:Current_events/2026_November_6&prop=links&format=json&formatversion=2"
```

### Labeling as a skill

The labeler sees what a human would: the page's short description and summary (REST
API `page/summary`), the burst's numbers, and the edit comments during the burst
(`prop=revisions` between `first_edit_at` and the end). The definition of "news" is
in `SYSTEM_PROMPT` in `label.py`. Writing it was the labeling lesson in miniature (see
"Unclear cases" below).

## Design decisions and deviations

- **Lead link = the first article link of each bullet**, at any depth. The portal nests
  entries: a top bullet names the ongoing story (`*[[Kivu conflict]]`), sub-bullets the
  day's development (`***The death toll ... [[Allied Democratic Forces]] ...`). Every
  bullet counts as an entry, so both the story article and the first link of the
  sentence are leads. The spec says "the first link in each portal entry"; this is the
  literal reading. Lead links are taken from the wikitext (`prop=wikitext`, besides the
  spec's `prop=links`) because `prop=links` is a flat, alphabetical list that has lost
  the page structure; they are then checked against the API's link list.
- **Redirects are resolved** (`action=query&redirects=1`, batches of 50), an addition to
  the spec: without it, portal links like *Tampa* or *Kenyan* could never match a spike.
- **"Within 24/48 hours of the portal date"** is measured between the spike's whole
  active period and the UTC day (see `gap_hours()`), so a long spike that started the
  evening before still counts. Portal days are fetched for two days on either side of
  the evaluated range; days in the future are not requested.
- **Recall is pooled** over all (day, article) pairs, not averaged per day, so a day
  with 100 links weighs more than a day with 9.
- **Label precision** = `news / (news + not news)`; `unsure` labels are counted and
  shown but left out. Labels are matched to spikes by title and overlapping active
  periods (`Label.covers()`), so one label serves every detector run that found the
  same burst.
- **Label precision is measured on the run's own random sample.** A first version
  counted every labeled spike of a run, but then the loose run's "sample" included the
  spikes sampled for the strict run (the strongest bursts), which biases the estimate.
  Now `metrics.sample_spikes()` (fixed seed 2026, size 50) draws the same sample in
  `label.py` and in `detection.py`, and precision is computed over exactly that sample,
  using whichever label covers each spike. A spike already labeled for another run
  isn't labeled twice; the existing label is reused.
- **Recall also uses spikes from around the evaluated days.** A story on the portal of
  5 November may spike on the evening of the 4th. The evaluation loads the run's spikes
  from two days before `--from` to two days after `--to`, uses all of them for recall,
  and only the ones that started inside the range for precision, labels and time to
  detection.
- **The labels file is append-only.** `label.py` never rewrites a line. The LLM skips
  spikes with a human label (or a label from the same model); `--manual` skips spikes a
  human already labeled and shows the LLM's verdict only *after* you answer, so it can't
  anchor you. When reading, a human label beats an LLM label (`pick_label()`).
- **LLM call without a new dependency.** Spec 04a will add `wikipulse.llm` with the
  `ollama` package; until then `OllamaLabeler` talks to Ollama's REST API (`/api/chat`)
  with httpx (already a dependency): JSON-schema `format`, `think: false`,
  temperature 0, `num_ctx` 8192 (Ollama silently truncates longer prompts otherwise),
  one retry on an invalid answer, a clear "run `ollama pull`" error for a missing model.
- **Model: `EVAL_LABEL_MODEL` defaults to `ollama:qwen3:8b` as the spec says, but the
  run used `--model ollama:qwen3:4b`**: only the 4b model is installed on this machine.
  Pull the 8b model (`ollama pull qwen3:8b`) and relabel to compare.
- **The prompt tells the model the burst's date is "today".** The first trial labeled
  *2026 United States Senate election in Alaska* "not news" because, to the model, 2026
  was in the future. It also called the burst on *2026–27 FA Cup* (first-round fixtures)
  "routine maintenance". After adding the date and "people tidy an article *because*
  it is in the news", both became `news` and the clear cleanup cases stayed `not news`.
- **Config snapshot.** Spike rows don't record the thresholds that produced them, so
  the report's config is the evaluation process's own `DETECT_*` configuration plus
  `--set` changes, which must match the detector run. A `detector_runs` table (run id,
  config, data range) would make this automatic; it would change the detector's code,
  which is out of scope here, so it is left for later.
- **`scripts/detect_recordings.py`** (not in the spec). Spec 02b needs spikes for whole
  days, and replaying two days through Redis at about 1,000 messages/s would take
  hours. The script runs the same `SpikeDetector` over the same filtered, deduplicated
  events (the 02a determinism check showed this gives identical spikes) and writes
  them to Postgres. A cheap `"enwiki" in line` pre-filter skips JSON parsing for other
  wikis without changing results; reading 66 hours of recordings took about 1.5 minutes.
- **`--note`** on `eval.detection` puts free text (here: which data the run saw) into
  the report.
- **`WikiClient` is synchronous.** One request at a time is the politeness rule anyway.
- Settings added: `WIKI_API_URL`, `WIKI_REST_URL`, `WIKI_API_MIN_INTERVAL_S`,
  `EVAL_LABEL_MODEL`, `OLLAMA_HOST` (all in `.env.example`).
- **User-Agent.** `.env` still holds the placeholder contact, and the recordings and API
  requests of this spec were sent with it. Set your real contact in `.env`.

## Results

All measured on 2026-11-07. Reports: `evals/results/2026-11-07_02b-*.md` (and `.json`).

**Data.** Three history recordings (`stream.record --since ...`) cover the whole stream
from 2026-11-04 12:00 to 2026-11-07 13:52 UTC without gaps (66 hours, about 15 GB, 366,552
English Wikipedia article edits, 259,224 by humans). The stream serves history much
faster than real time (17 hours took about 13 minutes), so no step needed scaling down.
The first 12 hours are warm-up for the baselines (on that half day the default detector
raised 44 spikes, against about 67 per full day later: the cold-start effect from 02a).
The evaluated days are **2026-11-05 and 2026-11-06**, both complete. 2026-11-07 was left
out because its portal page is still being written.

**Four detector variants** (`scripts/detect_recordings.py`, about 20 s each), evaluated
with `uv run python -m wikipulse.eval.detection --from 2026-11-05 --to 2026-11-06 --name
<run> --set <changes>`:

| Run | Start rule (short; long window) | Spikes | Recall, all links (of 208) | Recall, lead links (of 52) | Portal precision (lower bound) | Label precision (LLM, 50 labels) | Time to detection, median / p90 |
|-----|---|---:|---:|---:|---:|---:|---:|
| `02b-strict` | 6 edits, 4 editors; 12, 5 | 53 | 1.0% | 0.0% | 7.5% | 54% | 9.4 / 34.9 min |
| `02b-baseline` (defaults) | 5, 3; 10, 4 | 135 | 1.4% | 0.0% | 7.4% | 48% | 9.4 / 14.0 min |
| `02b-medium` | 4, 3; 8, 3 | 268 | 2.9% | 3.8% | 5.2% | 58% | 16.3 / 55.2 min |
| `02b-loose` | 3, 2; 6, 2 | 1,264 | 6.2% | 13.5% | 3.5% | 50% | 9.3 / 54.8 min |

`--compare 02b-baseline 02b-loose` prints: recall (lead) +13.5 pp, portal precision
-3.9 pp, spikes +1,129, and the four changed thresholds.

What the numbers say:

- **The trade-off shows in the portal metrics.** Looser thresholds raise recall (lead
  links 0% to 13.5%) and lower portal precision (7.5% to 3.5%).
- **Recall is low for a structural reason.** A separate check over the recordings
  showed that of the 52 lead articles on the two portal pages, only 21 received *any*
  human edit within the recall window (the day before to the day after), and only 11
  received at least 5 edits from 3 people *in total over those three days*. Portal
  stories are mostly international politics (Paraguay's impeachment, the Kivu
  conflict, Tanzania); English Wikipedia reacts to them slowly and with few editors.
  The loose detector finds the busiest: *2026 Paraguay banking crisis*, *2026 European
  unrest*, *2026 French student protests*, *Vice President of Paraguay*.
- **Portal precision is a very low lower bound (3.5 to 7.5%)**, while the LLM labels say
  about half of the spikes are news. The gap is news the portal never lists: US
  election results by state and candidate, a Zelda remake release, Indian reality-TV
  finals, football season pages, deaths, and *Susie Wiles* leaving as White House chief
  of staff.
- **Label precision barely moves between variants (48 to 58%).** With 50 labels the 95%
  margin is about ±14 pp, so these four numbers are statistically indistinguishable.
  They also hint at the detector's main noise: vandalism bursts (a vandal plus several
  people fixing by hand, not with "Undo") pass any of these thresholds. 21 of the 26
  "not news" labels in the baseline sample mention vandalism or reverts. A feature like "share
  of the burst's edits that were reverted" would likely help more than the thresholds.
- **Time to detection** (spikes on the portal): the median is about 9 minutes in every
  variant, the time a burst needs to reach the short-window counts. The p90 grows with
  looser thresholds because the long-window rule catches more slow stories.
- **LLM sanity check.** Of the 9 labeled spikes that are on the portal (so certainly
  news), the LLM called 8 news. The miss, *Adam Hamilton (pastor)* (elected US
  senator), was labeled `news` for an overlapping burst in another run's sample: the
  4b model is not fully consistent.

**Labels:** 155 LLM labels (`ollama:qwen3:4b`, thinking off, temperature 0) in
`evals/labels/spike_labels.jsonl`: 50 sampled from each run, 45 of them shared between
samples and labeled once. About 2.5 s per label; 295 MediaWiki/REST requests for the
context, cached in `data/cache/wikipedia/`. The portal needed 17 requests (5 pages, 12
redirect batches), cached in `data/ground_truth/`; repeated evaluations made none.

**Tests:** 48 new unit tests in `tests/eval/` (184 in total pass with `uv run pytest`)
and 1 new integration test (13 pass with `uv run pytest -m integration`). `ruff check`
and `ruff format --check` are clean.

**Baseline to beat** (for the devlog): defaults on 2026-11-05/06: 135 spikes, recall 1.4%
(lead 0.0%), portal precision at least 7.4%, label precision about 48% (LLM, n = 50),
time to detection 9.4 min median, 14.0 min p90.

### Unclear cases (from reading the labels)

The cases where the LLM's answer was debatable show where the definition of "news"
needs sharpening:

- **Pages that are busy because of news in general**: *Deaths in 2026*, *2026 in rock
  music*, *List of people executed in the United States*. Each edit is news-driven, but
  the burst is the page's normal flow. Probably "not news" as a *spike*.
- **Recurring events**: weekly reality-TV episodes (*Bigg Boss*, *The Amazing Race 39*),
  football season pages after each match. News to fans, never on the portal.
- **Cleanup after news**: the second burst on *2026 United States Senate election in
  Alaska* was reference formatting, a day after the vote-counting edits. Caused by the
  news, but no new event.
- **Future events**: *2026 Grand Slam of Darts* (starts a week later) was labeled news.
- **Plain mistakes**: *Shipley (surname)* and *Herbert Blomstedt* (his last concert was
  in May) were labeled news.

A sharper rule could be: "news = a new real-world development, within about two days,
that the burst's edits add or react to". Try `--manual` on the same sample and see where
you land.

## Check your understanding

1. The loose detector's portal precision is 3.5%, but the LLM says about 50% of its
   spikes are news. Which number would you report as "precision", and why are both
   needed?
2. Recall over lead links rose from 0% to 13.5% when the thresholds were loosened. Why
   can't any threshold setting get it close to 100% on this data?
3. The medium run's label precision (58%) is higher than the baseline's (48%). Does that
   mean the medium thresholds are more precise?

<details>
<summary>Answers</summary>

1. Neither alone. The portal number is a *lower bound*: every spike it counts is
   certainly news, but it misses everything the portal doesn't list. The label number
   is an *unbiased but noisy estimate* (a random sample of 50, about ±14 pp) from a
   labeler with its own errors. Report the label estimate as the best guess, with the
   lower bound next to it as a floor that needs no labeler at all.
2. Most lead articles barely get edited: only 21 of 52 had any human edit in the
   window, and only 11 had at least 5 edits from 3 people over three days. A detector
   of editing bursts can't see news that Wikipedia editors don't react to. The ceiling
   for this kind of detector on these two days is roughly 11 of 52, about 21%.
3. No. With 50 labels each, a 10-point difference is inside the ±14 pp margin of either
   estimate. To tell them apart you'd need many more labels, or a paired design: label
   only the spikes where the two runs differ.

</details>

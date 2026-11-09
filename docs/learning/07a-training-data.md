# Learning note: Spec 07a, Training dataset

## What was built

A versioned, labeled dataset of spikes for training the "is this spike real news?"
classifier of spec 07b, built from the lake's edit history with one command:

```
uv run python -m wikipulse.ml.dataset build            # writes datasets/v<n>/ to the object store
uv run python -m wikipulse.ml.dataset build --dry-run  # the same, but only reports
uv run python -m wikipulse.ml.dataset list             # the versions, rows and label balance
```

| File | Role |
|------|------|
| `wikipulse/lake/backfill.py` | `uv run python -m wikipulse.lake.backfill --since ... --until ...`: reads the stream's retained history (EventStreams `?since=`) over one connection, keeps the edits stage 1 keeps, and merges them hour by hour into the lake (`history/edits`). Holds the lake's writer lock; idempotent (merge by `event_id`). |
| `wikipulse/ml/features.py` | `SpikeWindow` (what the features may know about a spike), `PageEdit`, `PageSnapshot`, `compute_features()` (43 features in four groups), `FEATURES` (name -> meaning), and the leakage guard: `visible_edits()`, `usable_snapshot()`, `check_no_leakage()`, `LeakageError`. |
| `wikipulse/ml/pages.py` | The page *as it was at the cutoff*: `fetch_revisions()` (old revisions' wikitext, 20 per request), `RevisionCache` (one small JSON file per revision), `fetch_creation_times()` (first revision per page), `parse_wikitext()` (categories, templates, `<ref>` tags), `snapshot_of()`. |
| `wikipulse/ml/dataset.py` | The build: lake -> spikes (two detectors) -> features -> labels -> time split -> Parquet + manifest in `DatasetStore` (`<bucket>/datasets/v<n>/`). Also `list`, `--dry-run`, and `DatasetStore.read(version, split)` for 07b. |
| `wikipulse/eval/wiki_client.py` | `get_json(..., cache=False)`: a small addition so `pages.py` can keep its own compact cache instead of the multi-MB raw answers. |
| `wikipulse/config.py`, `.env.example` | `FEATURE_WINDOW_MIN`, `ML_LAKE_PREFIX`, `ML_DATASET_PREFIX`, `ML_CANDIDATE_DETECTOR`, `ML_WARMUP_HOURS`, `ML_VALID_DAYS`, `ML_TEST_DAYS`. |
| `tests/ml/` | 37 unit tests (features on a synthetic spike, the leakage guard, wikitext parsing, the API calls over a mock transport, splits, labels, versioned storage, a whole offline build) and 1 integration test (the build on the real RustFS and Postgres, own prefix and run ids). |
| `tests/lake/test_backfill.py` | 5 unit tests: the stage-1 filter, hours released only when settled, resume after a dropped connection, no duplicates on a re-run, the writer lock. |
| `evals/labels/spike_labels.jsonl` | 299 new LLM labels (appended; nothing rewritten), for spikes of the run `07a-candidates`. |

How the pieces fit:

```
EventStreams ?since=  ──  wikipulse.lake.backfill  ──>  wikipulse-lake/history/edits/year=/month=/day=/hour=
                                                              │  (plus the 06a recordings already archived there)
uv run python -m wikipulse.ml.dataset build                   ▼
 1. lake_events(): every edit, hour by hour, event-time order  (last contiguous stretch of hours)
 2. detect_spikes(): two SpikeDetectors on the same edits
        candidates (ML_CANDIDATE_DETECTOR) ──> Postgres run 07a-candidates   (what we classify)
        live defaults                      ──> Postgres run 07a-default      (07b's rule baseline)
    select_spikes(): drop the warm-up day and spikes whose feature window runs past the data
 3. page_edits(): every archived edit of those pages (one filtered scan)
    fetch_snapshots(): revision at each cutoff + page creation (MediaWiki API, data/cache/ml)
    check_no_leakage(compute_features(...))  for every row
 4. weak label: Portal:Current events within 48 h      (data/ground_truth, cached)
    02b label: evals/labels/spike_labels.jsonl (LLM or human, human wins)
 5. split_rows(): by UTC day:  train (all earlier days) | valid (1 day) | test (last 2 days)
 6. DatasetStore.write(): v<n>/train.parquet, valid.parquet, test.parquet, manifest.json
```

## Key concepts

### Data leakage: only what was known at prediction time

The classifier will run live (spec 07c): a spike starts, and `FEATURE_WINDOW_MIN` (10)
minutes later the model must say "news or not". So every feature must be computable at
that moment, the **cutoff** = `started_at + 10 min` (`SpikeWindow.cutoff`).

What *is* known at the cutoff: the burst that triggered the spike (from `first_edit_at`,
which can be up to an hour before `started_at` when the long-window rule fired), the
first 10 minutes after the start, the page's edits in the 24 hours before the burst, the
page's text as it was then, the title and the hour. What is *not* known: everything the
stored spike row says about its whole life. `total_edits`, `distinct_editors`,
`peak_edits_10m`, `score`, `ended_at` and `revert_edits` are final values, filled in as
the spike goes on. A model trained on `total_edits` would learn "big spikes are news",
score brilliantly on the test set (the test rows have their totals too), and then fail
live, where a 10-minute-old spike has no total yet. That is leakage: the features
quietly contain the answer's future.

Three places where it would have crept in here, and what stops it:

1. **The spike row's totals.** `compute_features()` never receives a `SpikeRecord`. It
   receives a `SpikeWindow` (title, `first_edit_at`, `started_at`, window), built by
   `window_of()`. The totals can't be used because they aren't there. The dataset
   doesn't store them either, so 07b can't pick them up by accident.
2. **Later edits.** `build_row()` hands the features *all* archived edits of the page
   (the dataset is built afterwards, so the future is sitting right there). The first
   thing `compute_features()` does is `visible_edits(edits, cutoff)`, the only door
   edits come through. `check_no_leakage()` then proves it for every row: it recomputes
   the features once with the later edits deleted and once with 40 invented later edits
   added (reverts, bots, new editors, a new page length, news words). Any difference
   raises `LeakageError` and the build stops. `test_features_ignore_everything_after_the_cutoff`
   shows it on a synthetic spike that goes on for 90 minutes after its cutoff, and
   `test_the_guard_catches_features_that_see_the_future` breaks the door on purpose
   (monkeypatching `visible_edits` to let everything through) and checks the guard fires.
3. **The page's current state.** Stage 3's enrichment stores categories as they are
   *today*. A person who died during the spike gets "2026 deaths" an hour later; a page
   tagged `{{Current}}` loses the tag a week later. So `pages.py` fetches the exact
   revision that was current at the cutoff (`revision_at()`: the newest `rev_new` among
   the page's edits up to the cutoff) and parses *its* wikitext. `usable_snapshot()`
   refuses a snapshot whose revision time is after the cutoff (`LeakageError`), and drops
   a page creation time after the cutoff (a page deleted and created again later).

Labels are the one place where the future is allowed: "was it on the portal in the next
two days?" is exactly the question the model learns to answer early.

### Weak supervision: a cheap imperfect label plus a small better one

Nobody labels thousands of spikes by hand. Two kinds of label are combined:

- **Weak label** (`weak_label`): the article is linked from `Portal:Current events` on a
  day within 48 hours of the spike (spec 02b's `match_spikes()`). It is automatic, free
  for every spike, and nearly always right when it says "yes". But it says "no" for most
  real news, because the portal lists 20 to 40 stories a day, mostly international
  politics, and hardly ever a football result, a TV final or a minor death. So a weak
  negative means "not on the portal", not "not news". It is also *provisional* for recent
  days: the portal pages of the last days are still being written, and those of the next
  two days don't exist yet (`weak_label_final` says whether every portal day it depends
  on was at least 3 days old when the dataset was built).
- **The 02b label** (`label_02b`): "news / not news / unsure" from spec 02b's labeler (an
  LLM, `ollama:qwen3:4b`, or a human; a human label always wins, `pick_label()`). Much
  closer to the real question, but only on a sample, and an LLM label has its own errors.

The trade-off: the weak label gives *volume* with *systematic* errors (its negatives are
biased toward everything the portal ignores); the labeled sample gives *quality* with
*small numbers* and random noise. `label` combines them in the simplest defensible way
(`combined_label()`): a portal link wins (1, source `portal`); otherwise a news / not
news label decides (source `llm` or `human`); otherwise 0 (source `not-on-portal`, the
weak negative). Every row keeps all three columns and `label_source`, so 07b can train
on the combination, evaluate on labeled rows only, or ignore the weak negatives.

### Feature engineering on a time series of edits

The 43 features (`FEATURES` in `features.py`, also in every manifest) come in four groups:

- **Burst shape** (`burst_features()`), over human edits from `first_edit_at` to the
  cutoff: how many edits and editors, how many before and after the start, edits per
  editor and the busiest editor's share (one person working vs. many people reacting),
  the busiest 10 minutes (`peak_count()`, a two-pointer pass), whether the editing is
  speeding up (`last_10m_share`), the median gap between edits, bytes added and removed,
  the share of anonymous editors (IPs and the temporary accounts `~2026-12345-67` that
  replaced them), of reverts, of minor edits, of empty edit summaries, and of summaries
  with news words ("died", "elected", "results", ...) or vandalism words.
- **History** (`history_features()`): edits and editors in the 24 hours and in the hour
  before the burst, hours since the previous edit, and the burst's rate relative to the
  page's normal rate (`rate_ratio`). A page edited 50 times a day isn't surprising at 10
  edits an hour; one edited once a month is.
- **Time and title** (`title_features()`): the UTC hour; whether the title has a year,
  the spike's own year ("2026 ... election"), a list prefix ("List of", "Deaths in"), or
  a disambiguator ("(film)").
- **The page at the cutoff** (`page_features()`, from `PageSnapshot`): page age in days,
  number of categories, "Living people", "<year> deaths", a category naming the spike's
  year, biography, a `{{Current}}` or `{{Recent death}}` template, and the number of
  `<ref>` tags. NaN when the revision's text is hidden or the page was deleted since.

Missing values are NaN, never 0: "no edits before the burst" (a real 0) and "we don't
know the page's categories" are different things, and tree models in 07b can use the
difference.

### Dataset versioning

A model must be traceable to the exact data it was trained on, or you can never explain
a change in its behaviour ("was it the code, the data, or the labels?"). `DatasetStore`
gives every build a new folder `v<n>` and never overwrites one:

- three Parquet files (`train`, `valid`, `test`) and `manifest.json`, written last, so a
  version without a manifest is incomplete and ignored (and its number is not reused);
- the manifest records the lake prefix and hour range read, the thresholds of both
  detector runs, the feature window and warm-up, the feature list with meanings, the date
  range, rows and label balance per split, the portal days and whether each was final,
  the SHA-256 of the labels file, the git commit (and whether the tree was dirty), the
  command line, and the SHA-256 of each Parquet file plus a `fingerprint` over all of
  them.

So 07b can log "trained on `datasets/v3`, fingerprint `ab12...`" as an MLflow tag, and
anyone can later check that the files are still the same. Two builds from the same lake,
labels and code produce the same fingerprint (`test_build_writes_a_versioned_time_split_dataset`
builds twice and compares), so the fingerprint identifies the *data*, while the version
number identifies the *build*.

### Time-based splits

`assign_splits()` sorts the UTC days of `started_at` and gives the last `ML_TEST_DAYS`
days to test, the `ML_VALID_DAYS` before them to validation, the rest to training. Never
random, for two reasons. The model will be used on *future* spikes, so it must be judged
on days after everything it learned from. And a random split leaks across time: one
story (an election, a death) makes several pages spike over a day or two, and with a
random split the model would meet the story in training and its sibling spikes in the
test set, which flatters it. Splitting by whole UTC days keeps each day on one side. (A
story running over midnight into the next split still crosses; 07b could leave a
one-day gap between splits if that turns out to matter.)

## Design decisions and deviations from the spec

- **What "the first 10 minutes of the spike" means.** The spike exists from `started_at`
  (when a start rule held); a live scorer can't act before that. So the prediction moment
  is `started_at + FEATURE_WINDOW_MIN`, and the features see the triggering burst before
  the start (from `first_edit_at`) plus the 10 minutes after it, and the page's past. The
  alternative, `first_edit_at + 10 min`, would sometimes lie *before* the spike was
  detected (the long-window rule fires up to an hour after the first edit), which a live
  model can't do. Recorded as an interpretation.
- **Which spikes are in the dataset.** Not the live detector's spikes, but a looser
  *candidate* detector (`ML_CANDIDATE_DETECTOR`: 4 edits from 3 editors in 10 minutes, or
  8 from 3 in an hour; 02b's "medium" variant) run over the lake. The classifier's job
  is to pick the news out of noisy candidates, so it should see the borderline cases the
  defaults reject; it also gives about twice as many rows (907 spikes vs. 453). The *loose*
  variant (3 edits from 2 editors) would have given ~5x more rows but mostly noise, and
  the per-spike API calls for the page snapshots would have taken hours. The live
  defaults run alongside (Postgres run `07a-default`), and `default_rule_fired` says
  whether they had fired for this burst by the cutoff: that is "the current rule-based
  detector" 07b must beat, computed without leakage.
- **Detection runs inside the build, from the lake**, not from recordings
  (`scripts/detect_recordings.py`) or the stream: the lake is the system of record now,
  and the same `SpikeDetector` gives the same spikes from the same edits in the same
  order. `lake_events()` sorts each hour by `(event_time, event_id)`, so the order (and
  the spikes) can't depend on how a file was written. Both runs are written to Postgres
  (replaced on each build) so the 02b label tool can sample them.
- **Warm-up and edges.** Spikes whose burst starts in the first `ML_WARMUP_HOURS` (24) of
  the data are dropped: the baselines are cold there (02a's cold start) and the 24-hour
  history features would be incomplete. Spikes whose cutoff lies after the data's end
  are dropped too. Only the last *contiguous* stretch of lake hours is used, so a gap
  can never silently corrupt baselines or history features (gaps are listed in the
  manifest).
- **Backfill straight into the lake** (`wikipulse/lake/backfill.py`, not in the spec's
  output list). The spec asks to "backfill with the stream's `since` parameter". The
  existing route (recorder to a file, `scripts/fill_stream.py` into Redis, archiver into
  the lake) writes the *whole* stream to disk first, about 230 MB per hour; a week
  would be almost 40 GB. The backfill command filters while it reads (the `"enwiki"` substring
  pre-filter, then stage 1's `is_wanted` and `normalize`) and merges each finished hour
  into `history/edits` (0.3 to 0.6 MB per hour). An hour counts as finished once the
  newest event time is 5 minutes past its end (the two data centres' topics arrive
  slightly interleaved). It holds the same Redis writer lock as the archiver, resumes
  with `Last-Event-ID` after a dropped connection (the server dropped it 6 times during
  the forward run and 3 times in the backward one), and a re-run merges without duplicates. The lock is renewed every
  third of its TTL while reading, not only before writes: near the live edge an hour
  takes longer than the 60 s TTL to arrive, and the first run lost (and re-took) its
  lock there.
- **How far back the stream goes: 7 days.** A first probe (`?since=` 25 October) answered
  with an event of 27 October and suggested two weeks of retention. That was misleading:
  only a few thousand stale messages of the inactive data centre's topic are that old.
  The real history, asked for on 9 November at 23:13 UTC, began on 2 November at 22:27
  UTC, about 7 days back. So the lake's history now covers **2 Nov 22:27 to 9 Nov 22:00
  UTC without a gap**: everything the stream still had. The backfill ran as
  `--since 2026-11-07T13:00Z --until 2026-11-10T00:00Z` (stopped by hand at the live
  edge after the 21:00 hour; 48 minutes) and then in one-day chunks backwards from
  4 November 12:00, newest first (30 minutes; the chunks before 2 November ended at once
  with nothing to keep). No step needed scaling down. The hours 4 Nov 12:00 to 7 Nov
  13:00 came from the 06a recordings and were merged, not replaced.
- **The first hour is partial.** History starts at 22:27, so the 22:00 hour has only 33
  minutes. It is warm-up anyway; the only effect is that spikes in the first half hour
  after the warm-up (3 Nov 22:00 to 22:27) see 27 minutes less of 24-hour history.
- **Page snapshots by revision, not by today's page.** See "Data leakage" above. One
  `prop=revisions&revids=...` request per 20 revisions (the API allows 50, but 20 old
  wikitexts of long articles already make answers of several MB), plus one request per
  page for its creation time (the API returns a page's first revision only one page at
  a time). All cached in `data/cache/ml/` forever (an old revision never changes), one
  request per second with the User-Agent, through 02b's `WikiClient`. Wikipedia answered
  HTTP 429 (too many requests) 3 times in about 2,000 requests (snapshots and labeling);
  `WikiClient` waited (honouring `Retry-After`) and retried. Revisions are cached one small JSON file per
  revision (`RevisionCache`, `data/cache/ml/revisions/`), not as raw answers: a first
  version cached whole batches, so one new spike shifted every batch and a rebuild asked
  for everything again, and the raw answers took 72 MB. That needed `cache=False` on
  `WikiClient.get_json()`, a small backwards-compatible addition. Categories come
  from the wikitext (`[[Category:...]]`), so categories added by templates are missed;
  `parse` with `oldid` would include them, but it re-renders the old page on Wikipedia's
  servers for every spike, which is not a polite thing to do thousands of times.
- **Labels: weak, 02b, and a combination.** See "Weak supervision". `combined_label()`
  lets a portal link override an LLM "not news" (the portal is the more reliable of the
  two when it says yes). "Unsure" labels are kept in `label_02b` but don't count.
- **Validation split, and its size.** The spec names train and test. 07b has to choose a
  threshold and compare models without peeking at the test set, so the build also holds
  out validation days between them. With 7 days of history, 3 test and 2 validation
  days would have left about one day to train on, so the defaults are `ML_TEST_DAYS=2`
  and `ML_VALID_DAYS=1`: train 3 to 6 November, validate on 7 November, test on 8 and 9
  November. Raise them once the live archiver has collected more days.
- **More LLM labels.** The spec says "the 02b label where available". Only 155 labels
  existed, all for 4 to 7 November, so the test days had none. I ran 02b's own labeler
  (`wikipulse.eval.label`, unchanged) on two random samples of the candidate run: 150
  spikes of 7 to 9 November (147 newly labeled) and 200 of 3 to 6 November (152 newly
  labeled). Same model as 02b (`ollama:qwen3:4b`, thinking off, temperature 0), so all
  labels in the dataset come from one labeler; `qwen3:8b` is installed and could relabel
  them as a comparison. The 299 labels were *appended* to `evals/labels/spike_labels.jsonl`
  (labeler `llm`, model recorded, run `07a-candidates`), nothing rewritten. Each took
  about 2 s plus two cached MediaWiki requests; 10 minutes in all.
- **Postgres side effects.** The runs `07a-candidates` (907 spikes) and `07a-default` (453)
  are in the `spikes` table and are replaced by every build. The 02b runs are untouched.
- **Traceability caveat.** The manifest of `v1` says commit `ac27528`, *dirty*: the code
  of this spec was not committed yet when it was built (the agent doesn't commit). After
  committing, a rebuild records a clean commit.
- **Where datasets live.** `<S3_BUCKET>/datasets/v<n>/` in the same RustFS bucket as the
  lake (the spec's "MinIO"; 06a explains why RustFS). The bucket's event notifications
  (06b) also fire for these Parquet files; the hourly-stats worker ignores keys outside an
  hour partition, so that is harmless.
- **No new dependencies.** pyarrow, httpx and the existing modules cover everything.

## Results

All measured on 9 and 10 November 2026 (UTC 9 Nov 22:00 to 23:35), on the human's
Windows machine with RustFS, Postgres, Redis and Ollama running.

### The history (acceptance criterion 2, first half)

| | |
|---|---|
| Lake prefix | `wikipulse-lake/history/edits` |
| Covered | 2 Nov 22:27 to 9 Nov 22:00 UTC, 168 hourly files, no gaps |
| Edits | 855,219 (594,264 by humans), no duplicate `event_id` |
| Newly backfilled | 2 Nov 22:27 to 4 Nov 12:00 (37 h) and 7 Nov 13:00 to 9 Nov 22:00 (57 h); about 14 million stream messages read, 1 hour 20 minutes in all |
| Check | 9 Nov 10:00 to 10:05: 312 edits in the lake, 308 in the API's `recentchanges` for the same 5 minutes |

Two checks that the lake is a faithful source. The default detector run over the lake
(`07a-default`) contains every one of the 202 spikes that 02b's `02b-baseline` found in
the recordings, with identical start times, and the candidate run every one of
`02b-medium`'s 404 (in the first build, which, like 02b, started at 4 Nov 12:00). And the
hours with fewer edits (2,700 to 3,000 at 08:00 to 11:00 UTC) are real: the API's own
`recentchanges` list agrees.

### Dataset `v1` (acceptance criterion 2, second half)

`uv run python -m wikipulse.ml.dataset build` took 130 s (detection over 855k edits
about 90 s; every API answer came from the cache by then). The earlier runs that filled
the cache spent about 10 minutes on API requests: 794 revisions in 40 requests and the
creation times of 587 pages, one request each.

```
v1  built 2026-11-09T23:32  commit ac27528b3a (dirty)  794 rows  2026-11-03 to 2026-11-09
  train    467 rows  4 days  weak+ 51 (10.9%)  02b labeled 259 (news 137, not news 121)  label+ 167 (35.8%)
  valid    111 rows  1 days  weak+ 9 (8.1%)  02b labeled 46 (news 27, not news 19)  label+ 35 (31.5%)
  test     216 rows  2 days  weak+ 20 (9.3%)  02b labeled 120 (news 66, not news 54)  label+ 79 (36.6%)
```

- **Size.** 907 candidate spikes were detected; 794 are in the dataset (113 fell in the
  first 24 hours of warm-up or ran past the end). 43 features. Train is 3 Nov 22:00 to
  6 Nov, validation 7 Nov, test 8 and 9 Nov. The Parquet files are 88, 38 and 53 KB.
  Fingerprint `14586b75...`.
- **Weak label balance.** 80 of 794 spikes (10.1%) are linked from the portal within 48
  hours. Strongly clustered: 36 of them on 4 November, the day after the US elections.
  `weak_label_final` holds for 196 rows (spikes up to 5 November): for the validation and
  test days the portal pages were not yet 3 days old, so their weak negatives are
  provisional.
- **02b label balance.** 425 rows have a label (54% of the rows): 230 news, 194 not news,
  1 unsure, all from `ollama:qwen3:4b`. 120 of them are in the test set.
- **Combined label.** 281 positives (35.4%): 80 from the portal, the rest from LLM news
  labels; the remaining rows are LLM "not news" or weak negatives (`not-on-portal`, 323
  rows).
- **Rule baseline.** The live default detector had fired by the cutoff for 366 of the 794
  candidates (46%).
- **Page snapshots.** 792 of 794 rows have their page as of the cutoff (one revision
  deleted since, one with hidden text); page features are NaN for the other two.
- **Leakage check.** `check_no_leakage()` passed on all 794 rows.

### What the labels say about each other (weak supervision in numbers)

| | LLM: news | LLM: not news |
|---|---:|---:|
| on the portal (weak +) | 29 | 4 |
| not on the portal (weak -) | 201 | 190 |

When the portal says yes, the LLM agrees 29 times out of 33 (88%); the 4 disagreements
are LLM mistakes or edge cases, which is why a portal link wins in `label`. But of the
230 spikes the LLM calls news, only 29 (13%) are on the portal. So a model trained on
the weak label alone would learn "portal-style news" (US elections, Paraguay's banking
crisis) and call most sports, entertainment and death news "not news". That is the
trade-off in one table.

### A first look at the features

Mean values by label (all 794 rows; weak = portal, LLM = 02b labels):

| Feature | weak + | weak - | LLM news | LLM not news |
|---|---:|---:|---:|---:|
| `prior_edits_24h` | 52.5 | 13.2 | 21.8 | 9.9 |
| `hours_since_prior_edit` | 2.4 | 11.2 | 6.7 | 14.6 |
| `has_current_template` | 0.44 | 0.07 | 0.13 | 0.02 |
| `cat_mentions_spike_year` | 0.69 | 0.35 | 0.67 | 0.01 |
| `title_has_spike_year` | 0.54 | 0.19 | 0.41 | 0.00 |
| `minor_share` | 0.11 | 0.25 | 0.11 | 0.39 |
| `vandal_word_share` | 0.01 | 0.08 | 0.03 | 0.14 |
| `anon_share` | 0.13 | 0.29 | 0.23 | 0.36 |
| `page_age_days` | 2,439 | 4,213 | 2,442 | 6,122 |

News pages were already busy the day before (stories build up), carry `{{Current}}`,
and are younger; non-news bursts have more minor edits, anonymous editors and
vandalism-fighting summaries (02b's suspicion confirmed). A warning sign for 07b: the
LLM labeled *no* spike with the year in its title "not news" (0.00). Either such pages
really are always news, or the labeler has a bias ("2026 ..." looks current), and a model
trained on LLM labels would copy that bias. That is a reason to evaluate on human labels
too (`uv run python -m wikipulse.eval.label --run-id 07a-candidates --from 2026-11-08
--to 2026-11-09 --manual`), and to read the test-set errors by feature.

### Tests and checks (acceptance criterion 1)

- Feature and leakage tests: `tests/ml/test_features.py` (18): every feature on a
  synthetic spike, history features, NaN for unknowns, page features, the leakage
  requirement (`test_features_ignore_everything_after_the_cutoff`), the cutoff boundary
  (an edit at the cutoff counts, one a microsecond later doesn't), the guard catching a
  feature that sees the future, a snapshot after the cutoff refused, a later creation
  time ignored.
- `tests/ml/test_pages.py` (6), `tests/ml/test_dataset.py` (13, including a complete
  offline build from a local lake, built twice: same fingerprint, new version number),
  `tests/lake/test_backfill.py` (5), and `tests/ml/test_dataset_s3.py` (1 integration
  test: the build on the real RustFS and Postgres, own prefix and run ids, cleaned up).
- `uv run pytest`: 901 passed, 1 skipped. `uv run pytest -m integration`: 66 passed,
  1 skipped. `uv run ruff check .` and `uv run ruff format --check .`: clean.

## Check your understanding

1. A teammate adds a feature `total_editors` taken from the `spikes` table's
   `distinct_editors` column and reports that the test-set precision jumped from 0.6 to
   0.9. What went wrong, and which part of this build would have stopped it?
2. Why is a spike that is *not* linked from the portal not simply a negative example,
   and what does `weak_label_final` add on top of that?
3. You rebuild the dataset tomorrow without changing any code or labels. Will you get the
   same version name? The same fingerprint? What would change the fingerprint?

<details>
<summary>Answers</summary>

1. `distinct_editors` is the spike's *final* count, known only when it has ended, hours
   after the cutoff at which the model has to decide. Big final spikes are mostly news,
   so the feature contains the answer; live, at 10 minutes, the value doesn't exist yet
   and the model collapses. That's leakage. Here the feature code never sees the spike
   row (`compute_features()` takes a `SpikeWindow`, and the dataset doesn't even store
   the totals); and any feature that reads edits after the cutoff makes
   `check_no_leakage()` raise `LeakageError` for every row of the build.
2. The portal lists a few dozen mostly political stories a day. Sports results, TV
   finals, most deaths and local news never appear there, so "not on the portal" is a
   *biased* negative: it is wrong exactly for the kinds of news the portal ignores. That
   is why the build keeps the 02b labels next to it and lets a news / not news label
   decide when the portal says no. `weak_label_final` adds time: the portal pages of the
   last few days were still being written (and those after the data's end didn't exist)
   when the dataset was built, so for recent spikes even a portal "no" may still turn
   into a "yes".
3. A new name for sure: every build gets the next version number (`v<n+1>`), and
   versions are never overwritten. The fingerprint (a hash over the Parquet files) is the
   same exactly when the rows are the same. With this data, probably not: the last
   days' portal pages keep changing and ripen into "final" over three days, so tomorrow
   some `weak_label` and `weak_label_final` values of recent spikes differ. Built twice
   from settled inputs, the fingerprint is identical (the offline test checks that). It
   also changes with any other input: new hours in the lake, labels appended to
   `spike_labels.jsonl`, a setting such as `FEATURE_WINDOW_MIN`, or a feature's code.

</details>

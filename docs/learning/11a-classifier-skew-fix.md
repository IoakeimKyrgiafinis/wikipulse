# Learning note: Spec 11a, Fix the news classifier's training/serving skew

## What was built

The news classifier is now trained on rows built the way `wikipulse.ml.serve` scores
spikes live: the same spikes (those of the default detector), with the same cutoff and
the same feature code.

```
uv run python -m wikipulse.ml.dataset build --mode serving --as-of 2026-11-15   # dataset v5
uv run python -m wikipulse.eval.label --run-id 07a-default --from 2026-11-09 --to 2026-11-10 \
    --size 1000 --model ollama:qwen3:4b                                         # labels
uv run python -m wikipulse.jobs.retrain train    --date 2026-11-09 --dataset v5        # v6
uv run python -m wikipulse.jobs.retrain evaluate --date 2026-11-09 --dataset v5 --challenger 6
uv run python -m wikipulse.jobs.retrain promote  --date 2026-11-09 --challenger 6
uv run python -m wikipulse.ml.drift --run-id 07a-default --since 2026-11-08 --until 2026-11-11 \
    --version 3 --reference-dataset v5
```

| File | Role |
|------|------|
| `wikipulse/ml/cutoff.py` (new) | The one feature path both sides use: `at_cutoff()` (the `SpikeWindow` and the page's revision at the cutoff), `features_at_cutoff()` (leakage-checked features with the snapshot), `revision_at()` (moved here from `dataset.py`). |
| `wikipulse/ml/serve.py` | `score_spikes()` and `model_inputs()` call `at_cutoff()` / `features_at_cutoff()`; `Prepared` now holds an `AtCutoff`. |
| `wikipulse/ml/dataset.py` | `build_row()` and `fetch_snapshots()` call the same functions. New *mode* (`--mode`, `ML_DATASET_MODE`): `serving` (default) takes the rows from the default detector; `candidates` is the 07a behaviour. In serving mode, `final_days()` and `assign_final_splits()` make the test split from the newest days whose portal is final. The manifest records `mode`, `rows_from_run` and `split.delayed_days`. |
| `wikipulse/ml/drift.py` | `--reference-dataset vN`: compare live scores with another dataset's training rows (here the new v5, before any model trained on it is champion). |
| `wikipulse/config.py`, `.env.example`, `docs/usage.md` | `ML_DATASET_MODE` (`serving` / `candidates`). |
| `tests/ml/test_cutoff.py` (new) | The shared-features test, the impossible-values test (and its control in candidates mode), the final-days split, and the delay of days whose portal isn't final. |
| `tests/ml/test_drift.py` | The `--reference-dataset` option. |
| `evals/labels/spike_labels.jsonl` | 186 new LLM labels (appended with `append_record` through `wikipulse.eval.label`; no line rewritten, no human label touched). |

How the pieces fit:

```
               lake history/edits
                      │
     SpikeDetector(default thresholds)  ── the same detector the live pipeline runs
                      │ spikes (run 07a-default)
        ┌─────────────┴──────────────┐
  dataset.build_row()          serve.score_spikes()
        │                            │
        └──── ml.cutoff.at_cutoff() ─┘   window: first_edit_at .. started_at + 10 min
              ml.cutoff.features_at_cutoff()   revision at the cutoff, check_no_leakage
                      │
        same 43 features, same default_rule_fired = True
```

## Key concepts

### Training/serving skew

A model learns a mapping from inputs to outputs *on the distribution of inputs it was
shown*. Skew is any difference between how training inputs and serving inputs are made.
Nothing crashes: the model gets numbers in the right columns and returns a score. The
scores are just worse, or differently calibrated, and nobody is told.

Here the skew came from *which* spikes became rows, not from the arithmetic. Datasets v1
to v4 used the looser candidate detector (4 edits from 3 editors in 10 minutes). For
the same burst, the candidate starts one edit earlier than the default detector (5 edits),
so its cutoff (`started_at + 10 min`) is earlier and every window feature is measured at
another moment. The clearest symptom: `edits_before_start` counts the burst's human edits
up to the start, and is at least 5 for any default spike, but 133 of the 379 rows of v4
where the default rule had fired have the value 4. The live scorer can never produce
that value.

Five spikes, old row (v4, candidate start) against new row (v5, default start):

| spike | start | `edits_before_start` | `edits_after_start` | `detect_delay_min` | `last_10m_share` |
|---|---|---|---|---|---|
| Stephen Kovacevich | 22:20 -> 22:21 | 4 -> 5 | 3 -> 2 | 8.8 -> 9.8 | 0.43 -> 0.29 |
| 2026 US House of Representatives ... | 00:32 -> 00:32 | 4 -> 10 | 4 -> 4 | 2.9 -> 38.9 | 0.50 -> 0.29 |
| Byron Donalds | 01:40 -> 01:41 | 4 -> 5 | 5 -> 4 | 7.6 -> 8.3 | 0.56 -> 0.44 |
| Keisha Lance Bottoms | 02:09 -> 02:10 | 4 -> 5 | 14 -> 13 | 3.0 -> 3.9 | 0.78 -> 0.72 |
| 2026 Georgia gubernatorial election | 02:20 -> 02:21 | 4 -> 5 | 6 -> 5 | 5.9 -> 6.7 | 0.60 -> 0.50 |

A minute's difference changes several features. The House election row shows a second
effect: the default detector fired on its *long-window* rule, so its burst begins 39
minutes before the start, while the candidate's burst begins 3 minutes before it.

**Why "share the feature code" is the standard fix.** If two pieces of code compute the
"same" feature, they drift apart: someone fixes a bug in one, or changes a window in the
other. Feature stores (Feast, Tecton and the feature platforms of large companies) exist
mainly so that training and serving read features from one definition. This project has
no feature store; `wikipulse/ml/cutoff.py` is its small version: `at_cutoff()` and
`features_at_cutoff()` are the only way either side gets features.
`test_dataset_and_serving_compute_identical_features_for_the_same_spike` builds a row with
`dataset.build_row()` and scores the same spike with `serve.score_spikes()` (same edits,
same page snapshot) and checks that all 43 features, the rule column and the model input
table are equal. `test_both_sides_call_the_shared_functions` replaces
`features_at_cutoff` with a spy and checks that both sides go through it.

Sharing code alone wasn't enough, because the code was mostly shared already: 07a and
07c both called `SpikeWindow(...)` and `check_no_leakage(...)`. The skew was in the
*inputs* (another detector's spikes). So the second half of the fix is the `serving`
mode. `test_serving_mode_rows_only_hold_values_serving_can_produce` builds a dataset from
a synthetic week of bursts at and around both thresholds (3 to 8 editors, slow
hour-long bursts). It checks three things:

- every row has `edits_before_start >= min(min_edits, long_min_edits)`;
- every row equals what `score_spikes()` computes for that spike;
- the pages are exactly those the live rule fires on.

The control test builds the same week in `candidates` mode and finds rows with the
value 4, including rows where the default rule had fired. Without the control, the
first test might pass only because the synthetic day was too easy.

### Label budgets

A new kind of row needs new labels. 07a's LLM labels were made for candidate spikes. A
label covers a spike when the title matches and the active periods overlap
(`Label.covers`), so many default spikes were already covered. Still, a dry run of v5
showed 39 of 72 test rows, 19 of 55 validation rows and 99 of 283 training rows without
a label.

**Budget: at most 200 LLM labels; 186 used.** Labelled in order of value:

1. **Test days (9 and 10 Nov), 47 labels.** Without at least 50 labelled test rows of both
   classes (`ML_PROMOTE_MIN_TEST_ROWS`), the comparison can't run at all.
2. **Validation day (8 Nov), 26 labels.** The challenger is chosen and its threshold set
   there.
3. **Training days (4 to 7 Nov), 113 labels.** More training data. I skipped 3 Nov: 47
   unlabelled spikes there, but almost all fall in the 24-hour warm-up and never become
   rows.

The labeler was `ollama:qwen3:4b`, not the current default `qwen3:8b`. All 454 earlier
LLM labels are from `qwen3:4b`. `needs_llm_label()` skips a spike only if a human or *the
same model* labelled it, so `qwen3:8b` would have labelled every covered spike again,
spending the budget on duplicates and mixing two labelers' biases in one dataset. The
labels came out 114 news and 72 not news. Cost: about 6 minutes of local GPU time
(roughly 2 s per spike) and 301 Wikimedia API requests for the labeler's context (71
more came from the cache), at one request per second with the User-Agent.

Meanwhile the human added labels by hand. 53 training rows of v5 now carry a human
label, which wins over the LLM (`pick_label`). The validation and test rows have none
yet, so the comparison below is "agreement with qwen3:4b", as in 07b.

### Honest comparisons when the data changes

The champion (v3) and the challenger (v6) were trained on different datasets (v2
candidates vs. v5 serving). Comparing each on its own test split would compare two
different questions. So both scored **the same 72 labelled test rows of v5**
(`compare_models()` in `wikipulse/jobs/retrain.py`, unchanged). Those rows are what
serving actually sees, so this is the comparison that matters for the live feed.

Is that fair to the old champion? Mostly yes:

- v3 was trained on v2's training days (3 to 6 Nov) and calibrated on 7 Nov. The v5 test
  days are 9 and 10 Nov, so it saw none of these spikes. `rows_seen_by()` reports 0, but
  it matches by `spike_id`, and default spikes have other ids than candidate spikes. So
  I checked the days by hand.
- The features are the same 43 columns, so v3 can score v5 rows without any adapter.
- One point in v3's favour: 07b chose v3's model family looking at v2's *test* split (8
  and 9 Nov). So 9 Nov is not completely new to the champion's selection.
- One point against v3: its threshold was set on candidate rows.

The challenger was chosen on the *validation* split (07c's rule against the winner's
curse), and the promotion margin (`ML_PROMOTE_MARGIN` = 0.01) was fixed before.

## Design decisions and deviations

- **A mode, not a new builder** (`--mode serving|candidates`). The rest of the build
  (splits, purge, as-of labels, manifest) stays one piece of code. Serving mode runs only
  the default detector, so a build no longer replaces the `07a-candidates` run in
  Postgres.
- **`serving` is the default** (`ML_DATASET_MODE=serving`). That way the weekly DAG's
  `build_dataset` also builds skew-free data. Otherwise the next weekly challenger would
  be trained on skewed rows again. `candidates` stays available, and with it the old
  versions can be rebuilt bit for bit (v1 to v4 are untouched and still readable).
- **The old offline-build tests now pin `mode="candidates"`.** They test the 07a
  behaviour (Quiet pages with 4 editors as rows), which is still the candidates mode. No
  assertion was changed or removed.
- **Requirement 4, "the test split is the newest days whose portal is final"**, applies
  in serving mode only (`assign_final_splits`). A day is final when every spike on it
  has a final portal (`portal_final` at the as-of moment). The test split is the newest
  `ML_TEST_DAYS` final days. Days after them that aren't final are *delayed*, which
  means left out of this version (`split.delayed_days` in the manifest). They are not put
  into training: training on days after the test days would break the time order. Candidates mode keeps its
  old split, so old builds stay reproducible. Spec 11b adds "and whose spikes are
  labelled" and a setting for the delay.
- **The as-of moment: `--as-of 2026-11-15`.** The lake ends at 10 Nov 14:00. By then the
  portal days for 9 and 10 Nov (up to 12 Nov) were not final, so a build "as of the end
  of the data" would have delayed the newest days. Fifteen November is the earliest moment at which the
  portal of 12 Nov counts as final (3 days), and it is not in the future, which
  `resolve_as_of` refuses. This is the existing "as of" bookkeeping. Nothing else in the
  build reads the clock (requirement 6).
- **`default_rule_fired` is True for every serving row.** `rule_fired()` finds the spike
  itself, and serving sets the column to True too. So the "rule" baseline is a constant,
  and its average precision is the base rate (0.514 on test).
- **Drift: `--reference-dataset`.** The drift report compares live scores with the
  *model's* training rows, so a rejected challenger has no live scores to compare. The
  spec asks to compare "the new training data with recent live scores". The new option
  reads v5's training rows and scores them with the model that scored the live rows
  (v3), so both sides have v3's scores and the features come from the new data.
- **"Recent live scores" are the default spikes of 8 to 10 Nov, scored by
  `serve backfill`.** These are the newest days in the lake. The `live` run has only 4
  scored spikes, too few for a drift report. `backfill` uses the same scoring path as
  `live` (`score_spikes()`), with edits from the lake. This is also the sample of
  07c's drift table, so before and after can be compared directly.
- **No lake extension and no live consumer.** The task ruled out a live consumer, so
  the dataset uses the lake as it was (2 Nov 22:00 to 10 Nov 14:00).
- **The ROADMAP follow-up is updated, not removed.** The code and data are fixed. But
  the champion is still the v2-trained model, so the weekly drift report (against the
  champion's own reference) keeps flagging `edits_before_start` until a serving-mode
  model wins a comparison.

## Results

**Dataset v5** (`mode: serving`, fingerprint `02f0c646...`, labels as of
2026-11-15T00:00Z, built in 72 s; 1 API request, 307 revisions from the cache).
- 485 default spikes in the lake, 422 selected after the warm-up and edges, 410 used.
- Purged at the split boundaries: 9 train and 2 validation rows (same page spiked again)
  and 1 validation row (still running). No delayed days.

| split | days | rows | known labels | news | not news | positive share | label sources |
|---|---|---:|---:|---:|---:|---:|---|
| train | 3 to 7 Nov | 283 | 278 (98%) | 151 | 127 | 54.3% | LLM 189, human 53, portal 36, unknown 5 |
| valid | 8 Nov | 55 | 55 (100%) | 38 | 17 | 69.1% | LLM 47, portal 8 |
| test | 9 to 10 Nov | 72 | 72 (100%) | 37 | 35 | 51.4% | LLM 62, portal 10 |

Before the 186 new labels (the dry run), the known labels were 184 / 36 / 33.

Check on the real data: v5 has no row with `edits_before_start < 5` (minimum 5). Of
v4's 379 rows where the default rule had fired, 133 have the value 4.

**Challenger v6** (logistic regression, chosen on validation; run `train v5`):

| model | valid AP | test AP | test precision / recall at its threshold |
|---|---:|---:|---|
| rule (constant here) | 0.691 | 0.514 | 0.514 / 1.000 |
| logistic regression (chosen) | 0.953 | 0.967 | 0.814 / 0.946 (threshold 0.170) |
| gradient-boosted trees | 0.940 | 0.940 | 0.679 / 0.973 |
| network + summary embedding | 0.865 | 0.906 | 0.795 / 0.946 |

**Comparison with the champion on the same 72 test rows of v5: rejected.**
- Champion v3: test AP 0.971, precision 0.897, recall 0.946.
- Challenger v6: test AP 0.967, precision 0.814, recall 0.946.
- Gap -0.003 (95% paired bootstrap -0.032 to +0.019), below the margin 0.01. v6 is
  tagged `status=rejected` with this reason, and v3 stays `@champion`.

The surprise: the skewed champion ranks the serving rows as well as the new model. Its
strongest inputs (07b: `cat_mentions_spike_year`, `news_word_share`, the page and title
features) don't depend on the exact start minute. The skewed burst features carry
little weight in a regularised logistic regression. So the skew cost little *ranking*
quality. It did make the drift monitor useless, because a real shift would hide behind a
flag that is always on. v6's lower precision comes from its threshold: it was set on a
validation day with 69% news, where a low threshold looks safe.

**Drift**: the same 130 scores (v3, default spikes with a cutoff from 8 to 10 Nov),
against two references:

| reference | rows | status | features drifted | `edits_before_start` PSI (floor) | score PSI (floor) |
|---|---:|---|---:|---|---|
| v2 train, default-rule rows (v3's own, before) | 152 | **drift** | 11 | **2.951** (0.219) | 0.158 (0.333) |
| v5 train (new, after) | 278 | **ok** | 2 | **0.076** (0.160) | 0.105 (0.242) |

`edits_before_start` no longer drifts. Neither do `last_10m_share`, `burst_minutes`,
`detect_delay_min` and `edits_after_start`: all four were window features skewed by the
earlier start. Two features still drift against v5: `page_age_days` (PSI 0.40) and
`bytes_added` (0.26). They describe *which pages* spiked on 8 to 10 Nov (older pages,
smaller edits), a real change in the news, not in how rows are made. The report against
v5 is stored in `drift_reports`.

On 02b-baseline (202 scores, 4 to 7 Nov) the PSI of `edits_before_start` drops from
2.855 to 0.047 and the drifted features from 3 to 0. Those days overlap v5's training
days, so this sample is partly in-sample and shows less than the 8 to 10 Nov one.

**Checks**: `uv run pytest` passed: 1,325 passed, 1 skipped, 79 deselected. `ruff check
.` and `ruff format --check .` are clean.

## Open points

- The champion is still the v2-trained model (see the ROADMAP follow-up). The next weekly
  retraining trains on serving-mode data automatically. Once a serving-mode model is
  champion, the follow-up can be removed.
- The test labels are all LLM or portal labels. Human labels on 9 and 10 Nov (`--run-id
  07a-default --manual`) would show how much of AP 0.97 is real.
- Spec 11b: the DAG's labelling task should label run `07a-default` (serving mode's rows),
  not `07a-candidates`.

## Check your understanding

1. The dataset and the scorer already called the same `compute_features()` before this
   spec. Where did the skew come from, then, and why did sharing the code not prevent it?
2. Why was the old champion still evaluated on v5's test rows, and not on the test split
   of its own dataset v2? What makes the comparison not quite perfect?
3. The champion scored as well as the new model, so why was the skew worth fixing at all?

<details>
<summary>Answers</summary>

1. From the *inputs*: the training rows were the candidate detector's spikes, which start
   one edit (often a minute) earlier than the default detector's spikes that serving
   scores. The same function, given a different `started_at`, measures a different
   moment: `edits_before_start` of 4 instead of 5, other `last_10m_share`, and so on.
   Sharing the function is necessary but not enough. The rows must also be the spikes
   serving scores. That is why the fix has two parts (`ml/cutoff.py` and `--mode
   serving`) and why the tests check both: equal features for the same spike, and no row
   serving couldn't produce.
2. Two models are only comparable on the same rows, and the rows that matter are the
   ones serving produces (v5's), so both scored v5's 72 labelled test rows. The champion
   never trained on those days (3 to 7 Nov vs. 9 and 10 Nov). But its model family was
   chosen in 07b looking at v2's test split, which includes 9 Nov, and its threshold was
   set on candidate rows. The first point favours it a little, the second hurts it a
   little.
3. Because monitoring was blind. Against its own training data, the champion's live
   inputs drifted every week (`edits_before_start` PSI 2.95, 11 features), so a real
   shift in the news would be lost in a flag that is always on. Against v5 the same
   scores are `ok`, and the two features that still drift describe a real change in
   which pages spiked. Also, "the ranking still works" was luck of this model family: a
   model that leaned on burst timing (the trees, the network) would have been hurt more,
   and the next model might be one of those.

</details>

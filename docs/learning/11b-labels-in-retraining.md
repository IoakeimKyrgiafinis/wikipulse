# Learning note: Spec 11b, Fresh labels for weekly retraining

## What was built

The weekly retraining DAG now labels its own new spikes, and it tests a challenger only
on days whose labels are in. Before, the newest spikes had no labels when the dataset was
built, so the test split (the newest days) was mostly unknown rows, and a run could be
rejected for "too few labeled test rows" although the data was new (dataset v4: 68
labeled and 105 unknown test rows).

```
check_drift >> extend_history >> label_new_spikes >> build_dataset
            >> train_challenger >> evaluate_against_champion >> promote_or_reject

uv run python -m wikipulse.jobs.retrain label --date 2026-11-09 --as-of 2026-11-15T18:00
uv run python -m wikipulse.jobs.retrain build --date 2026-11-09 --as-of 2026-11-15T18:00
```

| File | Role |
|------|------|
| `wikipulse/ml/fresh_labels.py` (new) | The labeling task's logic: `needs_label()` (nobody judged the spike: no label record of any labeler or model covers it, and the portal doesn't link it), `label_order()` (days that could be test days first, newest first; then newer days, oldest first), `label_within_budget()` (the loop: budget, time limit, Ollama or Wikimedia down), `plan_and_label()` and `LabelReport` (what was added, used, and is still unlabeled). |
| `wikipulse/ml/dataset.py` | `find_rows()`: the build's first half (lake, detector, warm-up, portal), now shared with the labeling task, so the task labels exactly the spikes the build will use. `is_labeled()`, `DayReadiness` and `day_readiness()`: the label delay. `build()` uses them for the test split and writes `delayed_rows`, `label_delay_days`, `label_min_share` and `days_not_ready` (per day, what it waits for) into the manifest. |
| `wikipulse/jobs/retrain.py` | New step `label` (`step_label`), `--as-of` for `label` and `build`, and the rejection reason `too_few_rows()`: how many test rows were labeled, unlabeled and delayed. `Comparison` has `unlabeled_rows`, `delayed_rows`, `delayed_days`. |
| `dags/retrain.py` | The task `label_new_spikes` between `extend_history` and `build_dataset` (in the Wikimedia pool), and the run parameter `as_of`. |
| `wikipulse/config.py`, `.env.example` | `ML_RETRAIN_LABEL_BUDGET`, `ML_RETRAIN_LABEL_MAX_MINUTES`, `ML_RETRAIN_LABEL_MODEL`, `ML_LABEL_DELAY_DAYS`, `ML_LABEL_MIN_SHARE`. |
| `docker-compose.yml` | `OLLAMA_HOST=http://host.docker.internal:11434` for the Airflow container: the task asks the Ollama on the host. |
| `tests/jobs/test_retrain_labels.py` (new) | Idempotence, human labels untouched, budget, time limit, invalid answers, Ollama down (through a mocked Ollama server), Wikimedia down, the label delay, the test split moving back and forward, the rejection message. |
| `tests/dags/test_dag_import.py`, `tests/jobs/test_retrain.py`, `tests/ml/test_cutoff.py` | The new task in the DAG; fixtures whose days had no labels now link their pages from the portal (see "Decisions"). |
| `docs/usage.md`, `docs/ROADMAP.md` | The commands; 11b done, its follow-up removed. |

How the pieces fit:

```
             lake (history/edits)          evals/labels/spike_labels.jsonl   portal
                     │                                  │                      │
          dataset.find_rows()  ── the same spikes ──────┼──────────────────────┤
          ┌──────────┴───────────┐                      │                      │
  label_new_spikes            build_dataset             │                      │
  needs_label: no label,      day_readiness(): a day is a test day only if     │
  no portal link              - it ended ML_LABEL_DELAY_DAYS before the as-of  │
  label_order: testable       - its portal is final                            │
  days first                  - >= ML_LABEL_MIN_SHARE of its spikes labeled    │
  budget + time limit         newest ready days = test; newer days wait        │
          │ appends            │
          └──► labels file ───►┘
```

## Key concepts

### Label latency

In most real systems the truth about a prediction arrives later than the prediction: a
loan defaults months later, a purchase is known days after a click, a human reviewer gets
to a flagged item next week. Here there are two sources of truth, and both are late:

- **The "Current events" portal.** A portal page keeps changing for a few days, so "not
  linked from the portal" means "not news" only once the page is final (3 days after its
  day begins, `FINAL_AFTER`). A spike's weak label reads portal days up to 48 hours after
  it, so a day's portal evidence is complete about 4 days after the day ends.
- **The LLM and human labels.** They exist only once someone asks. Before this spec nobody
  asked about the newest spikes, so they stayed unknown.

A pipeline can handle late truth in three ways: evaluate on whatever is known (biased: the
known rows are not a random sample, here mostly the portal-linked news), wait a fixed
time, or wait until the truth is in. `day_readiness()` in `wikipulse/ml/dataset.py` does
the last two together. A day can be a test day only when:

1. it ended at least `ML_LABEL_DELAY_DAYS` (4) days before the as-of moment (the fixed
   wait; set it higher to give a human time to label first),
2. every portal page its spikes depend on is final (`final_days`, from 11a), and
3. at least `ML_LABEL_MIN_SHARE` (95%) of its spikes are labeled (`is_labeled`: a portal
   link, or any human or LLM label, `unsure` included).

`assign_final_splits()` then takes the newest ready days as the test split. Newer days are
*delayed*: left out of this version, they become test days in a later week. They don't go
into training, because training on days after the test days would let the model learn
from its test's future. Older days that aren't ready go into training or validation like
the others (their unknown rows are dropped there, as before). `DayReadiness.waiting_for()`
names what a day waits for, and the manifest stores it (`split.days_not_ready`), with
`delayed_days` and `delayed_rows`.

You can see it on the real data. Before the labeling task ran, a dry-run build as of 15
Nov 18:00 logged `Delayed: 2026-11-10 waits for labels (31 of 87 spikes)` and made its
test split from 8 and 9 Nov. After the task had labeled them, 10 Nov was ready and the
test split became 9 and 10 Nov.

### Budgets in automated labeling

An LLM labeler costs time (here about 3 seconds per spike: two Wikimedia requests at one
per second, and a qwen3:4b call) and, with a paid model, money. A weekly job must not run
for an unknown time, so `label_within_budget()` in `wikipulse/ml/fresh_labels.py` has two
limits:

- **Count:** at most `ML_RETRAIN_LABEL_BUDGET` (150) spikes asked per run. An answer that
  is never valid JSON still counts (the call was made); a call that never happened (Ollama
  down) doesn't.
- **Time:** it stops after `ML_RETRAIN_LABEL_MAX_MINUTES` (40), checked between spikes,
  inside the task's one-hour Airflow timeout. Stopping by itself with the labels it has
  is better than being killed: a killed task fails, and a failed task stops the build.

With a budget, the order matters. `label_order()` labels the days that could be test days
first, newest first, because an unlabeled day there moves the whole test split back. Then
the newer days, oldest first (the next to become testable). Within a day, by start time.
(Choosing spikes by the model's *uncertainty*, active learning, is out of scope.)

Two more rules keep the budget from being wasted:

- **Idempotence.** `needs_label()` skips a spike any label record covers (any labeler, any
  model, `unsure` included) and a spike the portal links (already news). Inside a run,
  `label_within_budget()` checks again before each call, so a new label that also covers
  a second spike of the same burst saves that call (it happened once in the real run: 60
  spikes needed a label, 59 calls covered them all). Human labels are never touched: the
  task only appends to `evals/labels/spike_labels.jsonl` (`append_record`).
- **Never fatal for missing labels.** A `StructuredOutputError` skips one spike. An
  `LLMError` (Ollama not running, model missing) or an `httpx.HTTPError` from Wikimedia
  ends the labeling at once: retrying every spike against a server that is down helps
  nobody, and backing off is the polite thing to do with Wikimedia. The task logs what it
  did and returns 0, and the build goes on with the labels there are.

## Decisions and deviations

- **What `ML_LABEL_DELAY_DAYS` means.** The spec names the setting but not its meaning. I
  made it a fixed wait counted from the end of a day (default 4, what the portal needs
  anyway), on top of the two data checks (portal final, spikes labeled). With the default
  it rarely changes the split by itself; its use is to give human labelers time before a
  day is tested on. `lag_days()` uses it in the "lake too short" message.
- **A share, not "all spikes labeled"** (`ML_LABEL_MIN_SHARE`, 0.95, a setting the spec
  doesn't name). One spike whose answer is never valid JSON would otherwise hold a day
  back forever. The few unlabeled rows of a ready day are left out of the comparison and
  counted in the rejection message.
- **`unsure` counts as labeled** for readiness and for `needs_label`: asking the same
  labeler again gives the same answer. In the evaluation an `unsure` row is still
  unknown, as before.
- **"Newer days go to training or wait":** they wait (delayed). Days after the test days
  in training would break the time order; 11a made the same choice.
- **The task finds its spikes with the build's own code** (`find_rows()`, the first half
  of `build()` moved into a function). The spikes of run `07a-default` in Postgres are
  replaced by every build, so before the build they are last week's; detecting them again
  from the lake takes about two minutes and gives exactly the rows the build will use.
  The task doesn't store them in Postgres; the build still does.
- **The model is its own setting**, `ML_RETRAIN_LABEL_MODEL=ollama:qwen3:4b`, not
  `EVAL_LABEL_MODEL` (`qwen3:8b`, shared with the extraction judge and the grouping
  labeler). Every spike label so far is qwen3:4b, and the prompt is unchanged (out of
  scope). The 12b audit found this labeler over-calls news (all 10 disagreements with the
  human on 02b were LLM `news`, human `not news`), so the weekly labels carry that bias
  too; the ROADMAP follow-up on the labeler now says so.
- **`--as-of` for `label` and `build`, and an `as_of` DAG parameter.** The build could
  already take labels as of a later moment (11a used it); the DAG couldn't. It lets a run
  on an older lake test on days that are final *now*, as the acceptance run below does.
  Empty means the end of the lake, as before.
- **Fixtures.** Four existing serving-mode tests built from days without any label. Under
  the new rule no day was ready, so they failed. Their fixtures now link the pages from
  the portal (each spike becomes news), and `short_lake_build` got a `linked` flag; no
  assertion changed. The new tests use the unlinked version.
- **Airflow reaches Ollama** through `OLLAMA_HOST=http://host.docker.internal:11434` in
  `docker-compose.yml`, the way the Kubernetes pods (08a) reach it.
- **The acceptance run was smaller than a full week (the 60-minute rule).** The history
  lake ended on 10 Nov at 14:00. A full `extend_history` up to now (15 Nov, 20:00) reads
  about 150 hours of the stream's history, about 2 hours at 07c's speed. Instead I
  backfilled 10 hours by hand (`wikipulse.lake.backfill`, 10 Nov 14:00 to 11 Nov 00:00, 12
  minutes, one connection, the User-Agent from `.env`), marked `extend_history` as
  succeeded by hand in Airflow, and ran the rest of the DAG with `as_of =
  2026-11-15T18:00`. The other six tasks ran normally. The lake now ends on 11 Nov at
  00:00; the next weekly run's `extend_history` continues from there.

## Results

**The retraining cycle in Airflow** (run `spec11b-cycle`, week of 2026-11-02, conf
`run_id=02b-baseline`, `as_of=2026-11-15T18:00`). The first attempt failed in seconds: the
Airflow image was built on 10 Nov, before spec 09a added `prometheus-client` to
`docker/airflow/requirements-ml.txt`, so its ML environment couldn't import the detector.
I rebuilt the image (`docker compose --profile airflow build airflow`; no file changed),
cleared the tasks and ran them again: all succeeded, 20:54 to 21:01 UTC (7 minutes).

- `check_drift`: ok (202 scored spikes of 02b-baseline against v3's 152 training rows).
- `extend_history`: marked succeeded by hand (see above).
- `label_new_spikes` (3.5 minutes): 482 spikes can become rows; 422 already judged (a
  label or a portal link); **60 needed a label**. **Labels added: 59** (21 news, 38 not
  news) with qwen3:4b; **budget used: 59 of 150**; 0 invalid answers; **still unlabeled:
  0**. 112 Wikimedia API requests, 6 from the cache. 55 of the new labels are for 10 Nov
  (the afternoon the backfill added, and earlier 10 Nov spikes whose feature window used
  to run past the lake's end), 4 for 3 Nov.
- `build_dataset`: **dataset v6** (serving mode, labels as of 15 Nov 18:00), 470 rows of
  482 selected spikes; no day delayed, every day ready.

  | split | days | rows | known labels | positive share | label sources |
  |---|---|---:|---:|---:|---|
  | train | 3 to 7 Nov | 283 | 282 | 53.2% | LLM 184, human 62, portal 36, unknown 1 |
  | valid | 8 Nov | 55 | 55 | 69.1% | LLM 47, portal 8 |
  | test | 9 and 10 Nov | 132 | **132** | 47.0% | LLM 120, portal 12 |

  For comparison: v4 (07c) had 68 labeled and 105 unknown test rows; v5 (11a, with 186
  labels made by hand) had 72 test rows, all labeled. A dry run of the same build before
  the labeling task delayed 10 Nov (31 of 87 spikes labeled) and tested on 8 and 9 Nov
  (103 rows).
- `train_challenger`: logistic regression, chosen on validation (valid AP 0.956),
  registered as **v7** `@challenger`.
- `evaluate_against_champion`: "Test rows of v6: 132 labeled, 0 unlabeled; 0 rows on 0
  delayed days". **Reject**: test AP 0.948 (challenger v7) vs. 0.953 (champion v3) on the
  same 132 rows, gap -0.005 (95% -0.028 to +0.014), below the margin 0.01.
- `promote_or_reject`: v7 tagged `status=rejected`; v3 stays `@champion`. The DAG is
  paused again.

So the run ended in a documented rejection that isn't about labels: all 132 test rows were
labeled, and the gap is inside the noise. As with v6 in 11a, the serving-mode challenger
ranks about as well as the old champion, so the ROADMAP's skew follow-up stays open. These
test labels are LLM labels that the 12b audit found too eager to say news, so "AP 0.95"
means agreement with qwen3:4b, not with a human. (`rows_seen_by` can't tell whether v3
saw these days; it trained on 3 to 6 Nov, so it didn't.) v6's manifest says commit
`05b2605` "dirty": only the ROADMAP and this note were uncommitted then.

**The labels file**: 727 lines before, 786 after; the first 727 are byte for byte the same,
and the 59 new ones are `labeler: "llm"`, `model: "ollama:qwen3:4b"`, run `07a-default`.

**Tests**: 17 new tests in `tests/jobs/test_retrain_labels.py` and one in
`tests/dags/test_dag_import.py`. `uv run pytest`: 1367 passed, 1 skipped, 81 deselected;
the DAG tests inside the Airflow image: 34 passed; `ruff check` and `ruff format --check`
pass.

**Observed, not changed:** the 10-hour backfill logged "Lost the lake writer lock" after
two of its four stream reconnects. It finished, the lake is contiguous (194 hours) and no
lock was left in Redis, but the lock's renewal around a reconnect may deserve a look in
`wikipulse/lake/backfill.py`.

## Look at it yourself

- In Airflow (http://localhost:8080), run `spec11b-cycle` of `retrain`: the log of
  `label_new_spikes` shows each spike's label and reason, and the three summary lines.
- `uv run python -m wikipulse.ml.dataset list --json` shows v6's manifest:
  `split.days_not_ready` is empty, `labels.as_of` is 15 Nov 18:00.
- Some of the 59 new labels call sports results and TV episodes news. With the 12b audit
  in mind, would you agree? `uv run python -m wikipulse.eval.label --run-id 07a-default
  --from 2026-11-10 --to 2026-11-10 --manual` asks you.

## Questions to test understanding

1. Why does the build *delay* a day that isn't labeled yet, instead of testing on its
   labeled rows only?
2. The labeling task labels the days that could be test days first, newest first. What
   would go wrong with "oldest unlabeled spike first"?
3. Ollama is down during the weekly run. Walk through what happens in each task, and why
   the labeling task doesn't simply fail.

<details>
<summary>Answers</summary>

1. Because the labeled rows of a partly labeled day are not a random sample. Before this
   spec the known rows of the newest days were mostly the portal-linked ones, nearly all
   news; a test on them measures how well the model finds the obvious news, on few rows
   that change from week to week. Waiting until a day is (almost) fully labeled keeps
   the test split a fair picture of what the model meets live. The cost: the test split
   lags the newest data.
2. The budget would go to old training days first. With many old unlabeled spikes, the
   newest testable days might get no labels, and the build would move the test split
   back (or find too few ready days) although the run had budget for them. Labeling test
   candidates first makes the most useful labels arrive first; old training rows add only
   a little training data.
3. `check_drift` and `extend_history` don't use the LLM. `label_new_spikes` reads the
   lake, finds the spikes that need a label, and gets an `LLMUnavailableError` on the
   first call; it stops, logs "Stopped early: the labeler is unavailable ... The build
   goes on with the labels there are", and returns 0. `build_dataset` tests on the newest
   days that are ready with earlier weeks' labels; unlabeled days wait. Training,
   evaluation and promotion run as usual, and if even the delayed test split is too
   small, the rejection says how many rows were labeled, unlabeled and delayed. Failing
   the task would stop the whole week's retraining because of one missing service,
   although last week's labels may be enough to compare models.

</details>

# Learning note: Spec 07c, Model serving, monitoring and retraining

## What was built

The news classifier of spec 07b now does its job: it scores every spike ten minutes after
the spike starts, the scores show in the event feed and on the web page, a drift check
compares what it sees with what it learned from, and a weekly Airflow DAG retrains it and
promotes the new model only if it is measurably better.

```
uv run python -m wikipulse.db.migrate                       # tables spike_scores, drift_reports
uv run python -m wikipulse.ml.registry show                 # versions, aliases, status
uv run python -m wikipulse.ml.registry promote 3            # make version 3 the champion
uv run python -m wikipulse.ml.registry rollback             # back to the previous champion
uv run python -m wikipulse.ml.serve                         # live: score the live run's spikes
uv run python -m wikipulse.ml.serve backfill --run-id 02b-baseline   # past spikes, from the lake
uv run python -m wikipulse.ml.drift --run-id 02b-baseline --week-of 2026-11-02
docker compose --profile mlflow --profile airflow up -d     # then the DAG "retrain" at :8080
```

| File | Role |
|------|------|
| `wikipulse/ml/registry.py` | The aliases `champion` and `challenger`: `promote()`, `rollback()`, `rollback_target()`, `alias_version()`, `describe()`, and the rollback command. |
| `wikipulse/ml/serve.py` | `load_alias()` / `load_version()` (`LoadedModel`), `ModelHolder` (reloads when the alias moves), `score_spikes()` (features at the cutoff, then the model), `EditBuffer` + `LiveScorer` (live, from the Redis edit stream), `backfill()` (past runs, from the lake), `WikiPages` (page revisions through the 07a/07b caches). |
| `wikipulse/ml/drift.py` | `psi()`, `ks_statistic()`, `compare()` -> `DriftReport`, `reference_table()` (the champion's training rows), `current_table()` (the stored scores), `run_drift()`, the command. |
| `wikipulse/jobs/retrain.py` | The DAG's five steps (`drift`, `build`, `train`, `evaluate`, `promote`), `Comparison`, `compare_models()`, and the promotion rule `decide()`. |
| `dags/retrain.py` | Weekly DAG: `check_drift >> build_dataset >> train_challenger >> evaluate_against_champion >> promote_or_reject`. |
| `wikipulse/db/scores.py`, `migrations/0008_create_spike_scores.sql` | Tables `spike_scores` (one row per spike) and `drift_reports`; `spikes_to_score()`, `upsert_scores()`, `scored_spikes()`, `save_drift_report()`. |
| `wikipulse/db/events.py`, `wikipulse/events/feed.py`, `wikipulse/api/*` | The feed (`news 0.83` per page and event), the API (`news_score`, `is_news` on cards and pages), the web page (a green "news" badge, the score in the card and page lines). |
| `wikipulse/ml/train.py` | Small change: `register(..., supersede=False)` and `train_all(..., extra_tags=...)` for retraining. |
| `docker/airflow/Dockerfile`, `requirements-ml.txt`, `docker-compose.yml`, `dags/wikipulse_jobs.py` | A second environment in the Airflow image, `/opt/wikipulse-ml-venv`, with the whole project at uv.lock's versions (CPU-only PyTorch); `job_task(..., python=WIKIPULSE_ML_PYTHON)`; `MLFLOW_TRACKING_URI` and `HF_HOME` for the container. |
| `docker/mlflow/start.py`, `docker-compose.yml` | `--allowed-hosts`: MLflow 3 refused requests from other containers (see below). |
| `wikipulse/config.py`, `.env.example` | `ML_SERVE_*`, `ML_PROMOTE_MARGIN`, `ML_PROMOTE_MIN_TEST_ROWS`, `ML_RETRAIN_MODELS`, `ML_DRIFT_*`. |
| Tests | `tests/ml/test_registry.py` (7), `test_serve.py` (13), `test_drift.py` (10), `tests/jobs/test_retrain.py` (10), feed and API (2), `tests/dags` (DAG files, the ML requirements, the retrain DAG inside Airflow), `tests/db/test_scores_db.py` (4, integration). `tests/ml/registry_helpers.py` makes a throwaway SQLite registry. |

How the pieces fit:

```
                       MLflow registry: news-classifier
                       v1 v2 v3(@champion) v4(@challenger) ...
                          ▲ alias moves            │ load models:/news-classifier/<v>
       promote / rollback │                        ▼
 dags/retrain.py (weekly)                    wikipulse.ml.serve
  check_drift ──────────────┐                 live: Redis edit stream -> EditBuffer (26 h)
  build_dataset  (07a)      │                 backfill: lake history/edits
  train_challenger (07b) -> v<n> @challenger       │ spike's cutoff passed (event time)
  evaluate_against_champion: both score the        ▼ features (07a) + page at the cutoff
     same test rows -> decide()                spike_scores (score, is_news, version, features)
  promote_or_reject: @champion moves, or           │                     │
     status=rejected                               ▼                     ▼
                                       feed / API / web page    wikipulse.ml.drift
                                       ("news 0.83")            vs. the champion's training
                                                                rows -> drift_reports
```

## Key concepts

### The full model lifecycle

A model is not done when it is trained. The lifecycle, in this project's names:

1. **Train**: `wikipulse.ml.train` (spec 07b), or the DAG's `train_challenger`, fits four
   models on a dataset version and picks one by a rule fixed in advance.
2. **Register**: the chosen model becomes a numbered *version* of `news-classifier` in
   MLflow's registry, tagged with the dataset it came from.
3. **Deploy**: the alias `@champion` points at the version that serves.
   `wikipulse.ml.serve` loads `models:/news-classifier@champion` and writes every score
   with the version that made it.
4. **Monitor**: `wikipulse.ml.drift` compares the inputs and scores the model meets with
   its training data, once a week (the DAG's first task) or by hand.
5. **Retrain**: the DAG builds a fresh dataset from the lake and trains a *challenger*.
6. **Promote or roll back**: the challenger replaces the champion only if it beats it on
   the same test rows by `ML_PROMOTE_MARGIN`. If a new champion misbehaves in practice,
   `registry rollback` puts the previous one back in seconds.

The point of the cycle is that every step leaves a record: which data (dataset version
and fingerprint), which code (git commit in the manifest), which choice (selection rule,
decision run with its reason), which version scored which spike (`spike_scores.model_version`).

### Registry aliases and safe promotion

An alias is a movable pointer to a version. Serving never names a version number; it
asks for `@champion`. Promotion is one call (`set_registered_model_alias`), and so is a
rollback, so neither needs a restart, a rebuild or a copy of a model file.
`ModelHolder.current()` asks the registry every `ML_SERVE_RELOAD_S` (300 s) which
version `@champion` points at and loads the new one when it moved; the score rows then
carry the new version number. If the registry can't be reached, it keeps serving what it
has.

`promote()` records *where the alias came from*: the new champion gets the tag
`previous_champion=<old>`, the old one `status=retired`. `rollback()` follows that tag
(or, for a champion set by hand without a predecessor, takes the newest older version
that was never rejected or rolled back) and marks the abandoned version `rolled_back`, so
it is never picked as a rollback target again. Rolling *forward* is just `promote <v>`.

The promotion rule (`decide()` in `wikipulse/jobs/retrain.py`) is deliberately strict:

- **Same rows**: champion and challenger score the identical labeled test split of the
  *new* dataset (`compare_models()`), so the comparison can't be won by an easier test set.
- **Better by a margin**: test average precision of the challenger minus the champion's
  must reach `ML_PROMOTE_MARGIN` (0.01). A tie keeps the champion: a new model brings new
  behaviour and new risk, which only a real improvement pays for.
- **Enough evidence**: at least `ML_PROMOTE_MIN_TEST_ROWS` (50) labeled rows of both classes.
- **Fixed in advance**: the margin is a setting, logged as a parameter of the decision run,
  never chosen after seeing the numbers (the lesson of 07b's selection rule).
- **No stale decisions**: `promote_or_reject` refuses if `@champion` moved between the
  evaluation and the promotion (someone rolled back by hand in between).

The paired-bootstrap interval of the gap is computed and logged too, for the human reading
the decision; it does not decide (with 132 test rows, "significantly better" would almost
never happen, and the margin is the knob the spec asks for).

### Drift: when the data moves

A model learns the world as it was in its training data. When the world moves (an
election season, a holiday, a change in the detector), the inputs shift and the model's
errors grow silently: nothing crashes. Labels arrive late or never, so the first signal is
the *inputs* and the *outputs*:

- **Feature drift**: each of the 43 features in this week's scored spikes against the
  champion's training rows.
- **Prediction drift**: the distribution of the score, and the share flagged as news.

`psi()` computes the **population stability index**: bin the reference (deciles of its
values, or one bin per value for features with at most 10 distinct values, like the 0/1
flags; plus one bin for "unknown"), then sum `(q - p) * ln(q / p)` over the bins, with `p`
the reference share and `q` the current share (an empty bin counts as 0.0001). 0 means
identical; below 0.1 is stable, 0.1 to 0.25 a moderate shift, above 0.25 a large one.
`ks_statistic()` adds the Kolmogorov-Smirnov distance (the largest gap between the two
cumulative distributions). A period is `drift` when the score's PSI reaches `ML_DRIFT_PSI`
(0.2) or at least `ML_DRIFT_MAX_FEATURES` (5) features do, and `too_few_rows` below
`ML_DRIFT_MIN_ROWS` (30) scored spikes, because PSI on a handful of rows is noise.

The **reference** is the champion's own training data: the registry tag
`dataset_version` names it, and `reference_table()` reads that dataset's train split
(known labels, the rows the model was fitted on) and scores it with the champion. By
default only the rows where the live default rule had fired (`--reference default-rule`):
the live scorer only ever sees spikes of the default detector, so comparing with the
looser candidates would report the difference between two detectors every week.

## Design decisions and deviations from the spec

- **Scores in a table of their own, one row per spike** (`spike_scores`, keyed by the
  spike's id, `ON DELETE CASCADE`), rather than new columns of `spikes`. The detector
  rewrites its `spikes` rows every 30 seconds and knows nothing about models; the score
  has its own lifecycle (a version, a threshold, the features it saw, whether the history
  was complete). The feed and the API join it in, so for a reader it is "on the spike".
  The features are stored as JSON (unknown = null) because drift monitoring needs them.
- **When "after its feature window" is**: in *event time*. The live scorer scores a spike
  once the edit stream's newest event time is `ML_SERVE_LATENESS_S` (120 s) past the
  spike's cutoff (`started_at + FEATURE_WINDOW_MIN`). A replay therefore produces the same
  scores as the live run. The features are those of `wikipulse.ml.features`, computed
  through `check_no_leakage()` (so a page revision after the cutoff is refused), from the
  page's edits and its revision at the cutoff, exactly as in training.
- **Where the live scorer gets its edits: from the Redis stream itself.** Features need the
  page's last 24 hours, and the lake lags up to 5 minutes (the archiver's flush). So
  `LiveScorer` reads `wiki:edits` with plain `XREAD` (no consumer group: it acknowledges
  nothing and disturbs no one) into an `EditBuffer` of the last 26 hours per page. At
  start it reads 26 hours back from the newest entry (`stream_start_id()`), so a restart
  loses nothing. `EditBuffer.start` is where the buffer's knowledge begins: spikes whose
  burst began earlier are not scored, and a score whose 24-hour history reaches before it
  is stored with `history_complete = false`.
- **A backfill mode from the lake** (`serve backfill --run-id ...`), not in the spec, to
  score past runs: the live pipeline doesn't run all the time on this laptop, and drift
  needs scored spikes. It uses `dataset.page_edits()` on the lake, like 07a.
- **`default_rule_fired` is True for every served spike**: the live run's detector uses
  the default thresholds, so that rule has fired by the start of each of its spikes. Only
  the rule baseline reads this column. Scoring a looser run (e.g. `02b-loose`) would need
  it computed instead.
- **`summary_text` is fetched only for models that read it** (`LoadedModel.needs_text`,
  the network); for the others it is the title and costs no API request. Revisions at the
  cutoff come through 07a's and 07b's caches (`data/cache/ml/`), one request per 20
  revisions plus one per new page for its creation time, with the User-Agent and one
  request per second (`WikiClient`).
- **Lifecycle tags** next to the aliases: `status` is `challenger`, `rejected`, `champion`,
  `retired` or `rolled_back`. Retraining registers with `supersede=False`, so a new
  version no longer marks the champion "superseded" before it has won anything (07b's
  `latest`/`superseded` tags stay on versions 1 to 3 as history).
- **The challenger alias stays on a rejected version** until the next retraining moves it,
  so the last challenger and its decision stay easy to find; promotion removes it (the
  version is the champion then).
- **Retries are safe.** `train` looks for a version with the same `retrain_key` (the week)
  and dataset before training again (`existing_challenger()`); `build` makes a new dataset
  version (versions are cheap and never overwritten); `evaluate` writes a new decision run;
  `promote` does nothing twice. `promote_or_reject` has no retries: it fails only for a
  reason (no decision, the champion moved).
- **Values between tasks**: a BashOperator pushes the last line it prints as XCom; the
  next task gets it as an environment variable (`DATASET`, `CHALLENGER`), and the job
  checks its form (`v\d+`, `\d+`) before use, since it comes from another program's output.
- **Drift runs first and can't block retraining**: `check_drift` measures the champion
  that served the week before a promotion can replace it; `build_dataset` has
  `trigger_rule="all_done"`. Drift is a warning (`DRIFT:` log line and the stored report),
  not a failed task; stage 9 turns it into an alert.
- **A second Python environment in the Airflow image** (`/opt/wikipulse-ml-venv`). The jobs
  of 06c needed four packages; training needs MLflow, scikit-learn, PyTorch and
  sentence-transformers. `docker/airflow/requirements-ml.txt` is `uv export` of uv.lock
  (a test checks every pin against the lock and that every project dependency is there),
  PyTorch is installed first from the CPU-only index. The image grew from 3.3 GB to
  6.0 GB. The embedding model is cached in `data/cache/huggingface` (`HF_HOME`), not in
  the container.
- **MLflow refused the Airflow container.** MLflow 3 checks the `Host` header against DNS
  rebinding and by default allows only localhost and private IPs, so `http://mlflow:5000`
  got "Invalid Host header". `docker/mlflow/start.py` now passes `--allowed-hosts`
  (`MLFLOW_ALLOWED_HOSTS`: the published port on localhost and `mlflow:5000`). The first
  attempt of `check_drift` showed a second bug: `alias_version()` turned every registry
  error into "no champion". It now returns None only for "not found" and raises anything
  else (`test_an_unreachable_registry_is_an_error_not_a_missing_champion`).
- **Weekly interval**: Monday 00:00 to Monday 00:00 UTC (`CronDataIntervalTimetable("0 0 * * 1")`);
  the run for a week starts after it ends, `--date` is the Monday. The DAG is paused again
  after the acceptance run (like 06c's), see below.
- **The dataset is built from `ML_LAKE_PREFIX` (`history/edits`)**, which no process
  extends on its own: the live archiver writes `edits`. Until the backfilled history is
  extended (or `lake_prefix=edits` is passed once the live archive has days of data),
  weekly retraining sees the same edits every week. The DAG's `lake_prefix` parameter
  picks another prefix.
- **Stale tags.** A rollback drill showed that a version coming back as champion still
  carried `rolled_back_to` from an earlier state. `set_status()` now deletes
  `LEAVING_TAGS` (`retired_by`, `rolled_back_to`, `rejected_reason`) when a version
  becomes champion.

## Results

**Champion.** I promoted version 3 (logreg, dataset v2, test AP 0.938) with
`registry promote 3`. A rollback drill on the real registry went 3 → 2 (`rollback`) and
then 2 → 3 (`promote 3`). It ended with v3 @champion (`previous_champion=2`), v2
`retired` and v1 `superseded`.

**Scoring.**
- `serve backfill --run-id 02b-baseline` scored 202 spikes in about 12 s with 3
  revision API requests (the rest came from the cache). 98 of them were flagged as news.
- `serve live --run-id live --exit-when-idle 5` buffered 4,481 edits on 3,533 pages
  from the Redis stream. It scored the one spike whose cutoff had passed (Ramayana:
  Part 1, 0.617, news, `history_complete=false`, because the buffer started after the
  page's 24-hour history window). The other two spikes correctly wait for their cutoff.
- After scoring a 3-hour slice of `02b-loose`, `events.feed --run-id 03b-default-fix`
  shows the scores on events. Examples: "2026 United States Senate election in North
  Carolina … news 0.96" and "Frozen yogurt … 0.41".

**Drift.**

| Scored run, period | Rows (scored vs reference) | Status | Score PSI | Drifted features |
|---|---|---|---|---|
| 02b-baseline, week of 2026-11-02 | 202 vs 152 | ok | 0.088 | 4 of 43 (needs 5) |
| 07a-default, 2026-11-08 to 11-10 | 100 vs 152 | drift | 0.162 | 21 of 43 |

- The news share was 48.5% scored vs 57.9% in the reference.
- In both cases the largest PSI is `edits_before_start` (2.86 for 02b-baseline). This is
  **training/serving skew**: about 30% of training rows have exactly 4 edits before the
  start, which is the threshold of the *candidate* detector that built the training set.
  The live *default* detector needs 5, so that value never appears when serving. Drift
  monitoring found a real mismatch between how the training data was made and how the
  model is used.
- The 07a run also has smaller pages: median length 84k → 52k, references 135 → 74,
  categories 11 → 8.
- **Caveat:** with 100 vs 152 rows, PSI from sampling noise alone is about
  9·(1/n + 1/m) ≈ 0.15, close to the 0.2 threshold. A single feature over 0.2 on a small
  week is weak evidence, which is why the report needs `ML_DRIFT_MAX_FEATURES` features
  (or the score) to drift before it says `drift`.

**The retraining cycle in Airflow** (acceptance criterion 2). I ran the DAG `retrain`
as a backfill for the week of 2026-11-02 with conf `run_id=02b-baseline`. The run was
`backfill__2026-11-09T00:00:00+00:00`, 14:08–14:14 UTC, about 5.5 minutes in total.
- `check_drift`: ok. Its first attempt failed on MLflow's Host header check (see above).
  I cleared the task after the fix, and it succeeded.
- `build_dataset`: dataset v3, 768 rows, about 3 minutes. It has the same fingerprint as
  v2 (`fb6b5a57…`) because the lake has no new edits (see the open issue above).
- `train_challenger`: rule 0.614, logreg 0.938, gbt 0.958 test AP. The rule
  "simplest not clearly worse" picked logreg again (the interval for the gap to gbt,
  [-0.001, 0.046], reaches 0). It was registered as **v4 @challenger**.
- `evaluate_against_champion`: **reject**. "test average precision 0.938 (challenger
  v4) vs. 0.938 (champion v3) on the same 132 rows of v3: gap +0.000 (95% +0.000 to
  +0.000), below the margin 0.01". The two models are the same model trained on the same
  data, so a gap of exactly zero is the correct answer.
- `promote_or_reject`: tagged v4 `status=rejected` with the reason. v3 stays champion.

This is the "documented rejection" outcome the spec allows. The DAG was unpaused only for
this run and is paused again, so it doesn't run every Monday until the history lake is
extended.

**Tests.** There are new unit tests for loading and the model holder, scoring, the edit
buffer and live scorer (`tests/ml/test_serve.py`), PSI/KS and the drift report
(`tests/ml/test_drift.py`), aliases, promotion and rollback (`tests/ml/test_registry.py`),
and the promotion rule and steps (`tests/jobs/test_retrain.py`). All of them use
synthetic data and a local SQLite MLflow, and none touch the network. There are also
integration tests for the score tables (`tests/db/test_scores_db.py`) and DAG import
tests in the Airflow image.

## Questions to check your understanding

1. Why does the promotion rule compare champion and challenger on *the same* test rows
   (the challenger's test split), and why does it check that the champion wasn't trained
   on any of them?
2. The 02b-baseline drift report said `ok`, but `edits_before_start` had a PSI of 2.86.
   What caused it, and would retraining on newer data fix it?
3. What happens to live scoring if the MLflow server goes down for ten minutes? And if
   someone rolls back the champion during that time?

<details>
<summary>Answers</summary>

1. Average precision depends on the rows: an easy test set gives everyone a high score.
   Scoring both models on identical rows makes the gap measure the models, not the
   samples (and a paired bootstrap of that gap gives its interval). If the champion had
   been trained on some of those rows, it would have memorised them and look better than
   it is. `rows_seen_by()` counts them, and the comparison is logged with
   `champion_trained_on_test_rows` (0 in the acceptance run).
2. Training/serving skew. The training set came from spikes found by the looser
   *candidate* detector (4 edits before start counts), while live spikes come from the
   *default* detector (at least 5). Retraining on newer data built the same way would
   not fix it. The fix is to build training data with the detector that serves, or to
   make the feature independent of the detector threshold.
3. `ModelHolder` keeps serving the model it already loaded: a failed reload is logged and
   retried after `ML_SERVE_RELOAD_S`. Scores keep their model version, so they stay
   traceable. A rollback made during the outage takes effect at the first successful
   reload after MLflow is back. Until then the old champion keeps scoring, which is why
   the rollback isn't instant while the registry is down.

</details>

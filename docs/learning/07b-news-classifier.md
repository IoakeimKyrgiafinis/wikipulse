# Learning note: Spec 07b, News classifier with experiment tracking

## What was built

Four models that answer "is this spike real news?" ten minutes after a spike starts,
trained on the dataset of spec 07a, every run tracked in MLflow, and the chosen model
registered in MLflow's Model Registry:

```
docker compose --profile mlflow up -d               # the tracking server, http://localhost:5000
uv run python -m wikipulse.ml.train                 # newest dataset version, all four models
uv run python -m wikipulse.ml.train --dataset v2 --no-register --models rule,logreg
```

| File | Role |
|------|------|
| `docker/mlflow/Dockerfile`, `docker/mlflow/start.py` | The pinned MLflow server image (`ghcr.io/mlflow/mlflow:v3.18.0` plus the Postgres driver and boto3). `start.py` creates the `mlflow` database and the `wikipulse-mlflow` bucket if missing, then runs `mlflow server` with one worker. |
| `docker-compose.yml` | The `mlflow` service (profile `mlflow`, port 5000 on loopback only): metadata in the `mlflow` database of the existing Postgres, artifacts in RustFS. |
| `wikipulse/ml/models.py` | `ModelInput` (features, rule, embeddings), the four scorers (`RuleScorer`, `SklearnScorer` with `logistic_regression()` or `gradient_boosted_trees()`, `TorchScorer` with `NewsNet` and `train_net()`), and `NewsClassifierModel`, the MLflow pyfunc wrapper used for all four. |
| `wikipulse/ml/summaries.py` | The page summary as it was at the cutoff: `fetch_leads()` (old revisions' wikitext, cached per revision), `lead_of()` (wikitext to plain text), `summary_text()`, `embed_texts()`. |
| `wikipulse/ml/evaluate.py` | Metrics (`ranking_metrics`, `threshold_metrics`, `expected_calibration_error`), `choose_threshold()`, `bootstrap_interval()`, `paired_gap_interval()`, `permutation_importance()`, the plots, and `evaluate_model()` / `log_evaluation()`. |
| `wikipulse/ml/train.py` | The command: loads a dataset version, drops unknown labels, trains the four models in nested MLflow runs, evaluates them, writes the comparison, picks a model (`select_model()`) and registers it (`register()`). |
| `wikipulse/config.py`, `.env.example` | `MLFLOW_TRACKING_URI`, `ML_EXPERIMENT`, `ML_MODEL_NAME`, `ML_TARGET_PRECISION`, `ML_SELECT_MARGIN`, `ML_SEED`; Compose-only `MLFLOW_PORT`, `MLFLOW_DB`, `MLFLOW_BUCKET`. |
| `tests/ml/` | `synthetic.py` (a tiny synthetic dataset and a fake embedding model), `test_train.py` (12), `test_evaluate.py` (8), `test_models.py` (6), `test_summaries.py` (5), and `test_train_server.py` (1 integration test against the real server). |

New dependencies: `mlflow` (the client and the registry; the spec allows MLflow from this
stage), `scikit-learn` (logistic regression, gradient-boosted trees, the metrics; it was
already installed through sentence-transformers, now it is listed because we import it)
and `matplotlib` (the plots; mlflow needs it anyway). PyTorch was already a dependency
from the CPU-only index and stays CPU-only.

How the pieces fit:

```
RustFS  wikipulse-lake/datasets/v2/{train,valid,test}.parquet  (spec 07a)
   │  load_dataset()                    768 rows; label 1 / 0 / unknown
   ▼
known_rows()          unknown labels out (train 179, valid 52, test 84 rows)
add_summary_texts()   revision at the cutoff -> wikitext -> short description + lead
                      (MediaWiki API once, then data/cache/ml/leads/)
embed_texts()         bge-small-en-v1.5 -> 384 numbers per spike
   ▼
MLflow parent run "train v2"   (dataset version, fingerprint, row counts as params/tags)
 ├─ rule (live default detector)          no training; threshold fixed at 0.5
 ├─ logistic regression                   6 nested candidate runs (C)
 ├─ gradient-boosted trees                6 nested candidate runs (depth, trees)
 └─ neural network + summary embedding    1 nested ablation run (features only)
      each: evaluate_model() -> metrics + evaluation/*.png, *.json; pyfunc model "model"
 comparison.md / .csv, pr_curves.png, paired gaps to the best model
   ▼
select_model() -> mlflow.register_model() -> news-classifier v<n>
                  tags: dataset_version, dataset_fingerprint, dataset_commit, model_type
   ▼
MLflow server (Docker): metadata -> Postgres database "mlflow",
                        artifacts -> RustFS bucket "wikipulse-mlflow" (through the server)
```

## Key concepts

### Baselines first

The first "model" is the rule the system already has: the live detector's default
thresholds (5 edits from 3 editors in 10 minutes, or 10 from 4 in an hour, at 5x the
page's normal rate). 07a stored, for every candidate spike, whether that rule had fired
by the cutoff (`default_rule_fired`), so `RuleScorer` just returns that column as a score
of 0 or 1. It needs no training and its threshold is fixed at 0.5 (`fit_rule()` sets
`Fitted.threshold`), since "fired or not" is all it has.

The second baseline is logistic regression (`logistic_regression()` in `models.py`): a
weighted sum of the features pushed through a sigmoid, `p = 1 / (1 + exp(-(w·x + b)))`.
Before it sees the features, a small pipeline makes them comparable: `signed_log()`
squeezes long tails (a page age of 9,000 days and one of 3 days shouldn't differ by a
factor of 3,000), `SimpleImputer` fills unknowns with the training median and adds a 0/1
"was unknown" column for each feature that had any, and `StandardScaler` gives every
column mean 0 and spread 1, so the penalty `C` treats all weights alike.

Why bother with baselines? They tell you what "good" means here. The rule turned out to
be almost useless *for this question* (test average precision 0.614 against 0.598 for
random ranking): among spikes that look like spikes, the edit thresholds say little about
news. And logistic regression, the simplest learned model, came within 0.02 of the best
one. Without the two baselines, 0.958 for the trees would sound like a triumph of
boosting; with them, it is "about as good as a linear model".

### Experiment tracking

Every training session is one MLflow **parent run** (`train v2`) with a **nested run**
per model, and a nested run per hyperparameter candidate below those (`log_candidate()`),
so the UI shows a tree you can expand. What is logged where (`train_all()`,
`train_one()`, `log_evaluation()`):

- **Parameters** (fixed inputs): the dataset's row counts per split, how many rows were
  left out for an unknown label, the label sources (`llm:231,portal:48,unknown:179`), the
  seed, the target precision; per model its hyperparameters (`C`, `max_depth`,
  `max_iter`, the network's sizes, `best_epoch`).
- **Tags** (searchable labels): `dataset_version`, `dataset_fingerprint`,
  `dataset_commit`, `role` (comparison / model / candidate / ablation), `model_type`,
  and on the parent `chosen_model` and `chosen_reason`.
- **Metrics** (numbers): every metric on every split (`train_average_precision`,
  `valid_f1`, `test_brier`, ...), the threshold, latency, training time; the network's
  loss per epoch as a *step* metric (`epoch_train_loss`, `epoch_valid_loss`), which the
  UI draws as a learning curve.
- **Artifacts** (files): `evaluation/pr_curve.png`, `calibration.png`,
  `feature_importance.png` and `.json`, `metrics.json` per model; `comparison.md`,
  `comparison.csv` and `pr_curves.png` (all four models in one plot) on the parent; and
  the model itself (`model/`: the pickled `NewsClassifierModel`, its input signature,
  pip requirements, and the `uv.lock` MLflow copies in when it detects a uv project).

To compare side by side, open the experiment `news-classifier`, tick the four runs with
the tag `role = model`, and press *Compare*: parameters and metrics line up in columns.

The registry then holds *which* model is in use, apart from all the runs that produced
candidates: `register()` calls `mlflow.register_model()` on the chosen run's model and
tags the new version with the dataset version and fingerprint, so `news-classifier`
version 2 can always be traced to `datasets/v2` (fingerprint `fb6b5a57...`), which itself
records the git commit it was built from (`c498214`, clean).

### PyTorch basics

`NewsNet` (in `models.py`) is small on purpose. Its inputs are the 43 scaled features
(plus a "was unknown" column for each feature with gaps in the training rows; v2's 279
labeled training rows have none) and the 384-number summary embedding, which first goes
through a narrow `nn.Linear(384, 8)` (`project`), so that 384 numbers can't drown 43
features with only 279 training rows. Then one hidden layer of 32 with ReLU and dropout
0.2, then one output, a *logit* (any real number; the sigmoid makes it a probability).

`train_net()` is the training loop, and every line is one idea:

```python
for epoch in range(config.max_epochs):
    net.train()  # dropout on
    order = torch.randperm(len(labels), generator=shuffler)
    for start in range(0, len(order), config.batch_size):
        batch = order[start : start + config.batch_size]
        optimizer.zero_grad()  # forget the last batch's gradients
        logits = net(features[batch], embeddings[batch])  # forward pass
        loss = loss_fn(logits, labels[batch])  # BCEWithLogitsLoss
        loss.backward()  # gradient of the loss per weight
        optimizer.step()  # Adam: a small step downhill
    net.eval()  # dropout off
    with torch.no_grad():  # no gradients needed to measure
        valid_loss = loss_fn(net(valid[0], valid[1]), valid[2]).item()
    # keep a copy of the weights whenever valid_loss is the best so far;
    # stop after `patience` (30) epochs without improvement
```

Three things keep a network this size from memorising 279 rows: *weight decay* (Adam's
`weight_decay=1e-3`, a pull of every weight toward 0), *dropout* (a random fifth of the
hidden units switched off at each step, so no unit can rely on another), and *early
stopping* on the validation loss (the real run was best at epoch 22 of 53 and kept those
weights). `torch.manual_seed(seed)` and a seeded `Generator` for the shuffling make the
run reproducible (`test_training_loop_learns_and_is_reproducible` trains twice and gets
the same scores).

### Model evaluation: precision-recall, thresholds, calibration

**Precision-recall curve.** A model gives scores; every threshold turns them into
decisions, with a precision (of the flagged spikes, how many are news) and a recall (of
the news spikes, how many are flagged). Sliding the threshold traces the curve
(`plot_pr_curves()`); *average precision* summarises it in one number (1.0 = perfect,
the share of positives = random). It is the main comparison metric here because it
doesn't depend on a threshold and, unlike ROC AUC, focuses on the positive class, which
is what the feed shows.

**Choosing the threshold for the use case.** The classifier will mark events in the
live feed (spec 07c). A feed that flags non-news as news loses trust fast, so
`choose_threshold()` takes the threshold with the most recall among those that reach
`ML_TARGET_PRECISION` (0.8) on the *validation* split; if none reaches it, the one with
the best F1. Chosen on validation, reported on test, never the other way round. For
logistic regression that gave 0.438; on the test split it flags 81 of 132 spikes with
precision 0.864 and recall 0.886.

**Calibration.** Does a score of 0.7 come true 70 % of the time? `plot_calibration()`
draws the reliability diagram (mean score against the share of news per score bin),
`expected_calibration_error()` averages the gaps (weighted by how many spikes fall into
each bin), and the Brier score is the mean squared error of the score. Calibration
matters when a score is shown to people or read as a probability; for ranking alone it
doesn't. The rule is the extreme case: its "probabilities" are 0 and 1, and its Brier
score (0.477) is far worse than always answering 0.6 (0.24).

**Uncertainty.** The test split has 132 labeled spikes, so every number wobbles.
`bootstrap_interval()` resamples the test rows 1,000 times and reports the 2.5th and
97.5th percentiles of average precision. `paired_gap_interval()` does the same for the
*difference* between two models, scoring both on the same resampled rows each time, so
the luck of the sample cancels out. That paired interval decides the model (below).

**Feature importance.** `permutation_importance()` shuffles one input at a time across
the test rows (which breaks its link to the label but keeps its values) and measures how
much average precision drops. It works the same for all four models, and treats the
summary embedding as one block (`summary_embedding`).

## Design decisions and deviations from the spec

- **RustFS, not MinIO, for artifacts.** The spec says "artifacts in MinIO". MinIO no
  longer publishes images (see 06a), so the artifacts go to the project's S3 store,
  RustFS, in a bucket of their own (`wikipulse-mlflow`), not the lake's. The server
  *proxies* artifacts (`--serve-artifacts --artifacts-destination s3://wikipulse-mlflow`):
  the training code uploads to the server, which writes to S3. So the client needs no S3
  credentials and no boto3, and the bucket's 06b notifications (configured on
  `wikipulse-lake` only) never see model files.
- **The MLflow server image is built, not pulled as is.** The official image is pinned
  (`ghcr.io/mlflow/mlflow:v3.18.0`) but contains no Postgres driver and no boto3, so
  `docker/mlflow/Dockerfile` adds both, pinned (`psycopg[binary]==3.3.8`,
  `boto3==1.43.127`). SQLAlchemy 2.1 in the image uses psycopg 3 for `postgresql://`
  URLs; the first try with psycopg2 failed at startup for that reason. The image
  `-full` tag exists too, but brings everything MLflow can use, at several times the size.
- **Lean on RAM.** The first start used 1.9 GB: MLflow 3.18 starts a job runner with
  nine background processes (about 250 MB each) for GenAI features (online scoring of
  LLM traces, trace archival). `MLFLOW_SERVER_ENABLE_JOB_EXECUTION=false` turns it off,
  and `--workers=1` keeps one web worker: about 400 MB now. The service is behind a
  Compose profile (`mlflow`), like Airflow, so `docker compose up -d` doesn't start it.
- **Metadata in its own database.** `start.py` creates the `mlflow` database in the
  existing Postgres server (as `docker/airflow/start.py` does for `airflow`); MLflow
  creates its tables there. WikiPulse's own database is never touched.
- **Unknown labels are left out, and counted.** v2 has 768 rows, but `label` is unknown
  for 315 of them (train 179, valid 52, test 84). They are not in training, threshold
  choice or evaluation (`known_rows()`); the counts are logged as `*_excluded_unknown`
  params and printed. Treating them as 0 would teach the model that "nobody labeled it"
  means "not news", the mistake 07a's review removed.
- **The labels are LLM labels.** Of the 453 known labels, 382 come from the 02b labeler
  (`ollama:qwen3:4b`) and 71 from portal links; none are human. So every number below
  measures agreement with *that labeler* (and the portal), not with human judgement. The
  parent run is tagged `labels = llm+portal`, and `label_sources` is a parameter of each
  split.
- **Which summary is embedded: the page as it was at the cutoff.** Stage 3 stores
  today's summary, and today's summary of a person who died during a spike says so: an
  embedding of it would leak the label. `wikipulse/ml/summaries.py` reuses 07a's idea: it
  fetches the wikitext of the revision current at the cutoff (`page_revision_id`, 20 per
  request through `WikiClient`, User-Agent, one request per second; 39 requests per
  parse version, then the cache) and takes the `{{Short description}}` and the lead
  section. The text embedded is `page_text(title, description, lead)` with the existing
  `BAAI/bge-small-en-v1.5` (`EVENTS_EMBED_MODEL`), as in event grouping. `lead_of()` is a
  regex approximation of wikitext (templates, tables, refs, files and comments dropped,
  link text kept); it found a lead for 767 of 768 revisions (one has hidden text, so
  that spike's text is its title only). The cached leads carry a parser version
  (`LEAD_PARSER`): the first version of `lead_of()` left image captions ("thumb|...") in
  the text, and bumping the version made the cache re-parse instead of serving stale
  text.
- **Threshold-free selection metric and a statistical tie-break.** The spec asks to
  choose by test-set performance, weighing simplicity and speed. `select_model()` takes
  the *simplest* model (rule, logistic regression, trees, network) whose paired-bootstrap
  95 % interval of the gap in test average precision to the best model reaches
  `ML_SELECT_MARGIN` (default 0: "the test set can't tell it is worse"). The first
  version used a fixed margin (0.02) instead; on v2 the gap between trees and logistic
  regression was 0.0201, so the choice hung on the fourth decimal. The paired interval
  asks the better question: can 132 test spikes tell these two apart at all? That first
  session registered the trees as `news-classifier` version 1; the final session
  registered logistic regression as version 2. Both stay in the registry (versions are
  history); spec 07c decides which one gets the `champion` alias.
- **Choosing on the test set.** The spec says to choose by test-set performance, so the
  test split is also the selection set: its numbers for the *chosen* model are slightly
  optimistic. Hyperparameters, thresholds and early stopping use only the validation
  split, and the validation split agrees with the ranking (trees 0.994, network 0.978,
  logistic regression 0.974, rule 0.616), so the choice doesn't depend on test luck.
- **Hyperparameters by validation log loss**, not validation average precision: with 42
  validation rows, a ranking metric jumps with every swapped pair, while log loss uses
  every score. Small grids: six `C` values (0.003 to 1) and six tree settings (depth 2 or
  3, 50/150/300 trees, learning rate 0.05, at least 10 rows per leaf, L2 1.0). No
  retraining on train + validation: then the threshold, chosen on validation, would come
  from rows the model had seen.
- **Gradient-boosted trees from scikit-learn** (`HistGradientBoostingClassifier`), not
  XGBoost or LightGBM: no new dependency, and it handles NaN natively, which 07a's
  features use for "unknown".
- **One model format for all four.** Every model is logged as the same pyfunc,
  `NewsClassifierModel`: a table with `INPUT_COLUMNS` (the 43 features,
  `default_rule_fired`, `summary_text`) in, `score` and `is_news` (score at or above the
  model's validation threshold) out. Spec 07c can serve whichever version is champion
  without knowing its type. The network embeds `summary_text` itself (loading the
  embedding model on first use, never pickling it: `__getstate__`). The pickled model
  refers to classes in `wikipulse.ml.models`, so loading needs the `wikipulse` package
  installed (true for everything in this repository). The pip requirements are listed
  explicitly (`pip_requirements()`), so MLflow doesn't start a subprocess to guess them.
  MLflow warns that no input example was logged; the signature is logged instead, since
  an input example would make MLflow run the network, and with it load the embedding
  model, at logging time.
- **An ablation for the network.** "Features plus a summary embedding" raises the
  question whether the embedding helps. A nested run `torch features only` trains the
  same network without it: test average precision 0.906 instead of 0.948. It is logged
  for information and never registered.
- **Speed.** Latency is measured per spike in one batch (`latency_ms()`): all three
  learned models score a spike in well under 0.1 ms, so speed doesn't separate them. What
  costs time is the network's input: the summary needs the page's wikitext (one API
  request) and an embedding (`embed_ms_per_spike`, 84 ms per spike in this run,
  including loading the model once). That counts against the network for live use.
- **Experiment hygiene.** Nested runs inherit the *active* experiment, not the parent's:
  a first version passed the experiment only to the parent, and the children landed in
  "Default". `train_all()` now calls `mlflow.set_experiment()` first, and
  `test_all_four_models_are_trained_and_logged` checks the experiment of every child.
  The stray runs of those first tries were deleted and garbage-collected (`mlflow gc`).
- **Windows console.** MLflow prints run links with an emoji, which crashed the first
  real run at its end when stdout was redirected (cp1252). `main()` switches stdout and
  stderr to UTF-8.

### Tests and CI

- Unit tests (`uv run pytest`) use a throwaway **SQLite** MLflow store and artifact
  folder under pytest's temporary directory (`tracking` fixture in `test_train.py`), a
  synthetic dataset (`tests/ml/synthetic.py`: 300 rows whose label depends on a few
  features and the summary text, with unknown labels and NaN features) and a fake
  embedding model (`FakeEncoder`), so they need no server, no network and no download.
  They run in CI. The module-scoped `report` fixture trains all four models once (about
  half a minute) and the tests then check the runs, metrics, artifacts, the registered
  version's tags, and that every logged model loads and scores a table.
- `tests/ml/test_train_server.py` (`-m integration`) trains three models against the
  real server, checks the registry and that artifacts come back through the proxy from
  RustFS, then deletes its own experiment and registered model (names with a random
  suffix). It **skips** when no server answers at `MLFLOW_TRACKING_URI`; CI doesn't start
  one (the image is built locally, and GitHub's service containers can't build), so in
  CI it reports as skipped. Deleting an experiment in MLflow is a soft delete: its rows
  and artifacts stay until `mlflow gc` runs inside the container.

## Results

Measured on 10 November 2026 on the human's Windows machine (CPU only), MLflow server in
Docker, dataset `v2` (built from the clean commit `c498214`, fingerprint `fb6b5a57...`).
The final session is the parent run `train v2` with id `01d64af9...` in the experiment
`news-classifier` (http://localhost:5000, experiment 2). `uv run python -m
wikipulse.ml.train --dataset v2` took about 2 minutes with the summaries cached: 15 s
loading the dataset, 25 s loading the embedding model and embedding 453 summaries, and
1 minute training, evaluating (1,000 bootstrap rounds and permutation importance per
model) and uploading. Fetching the 768 revisions' wikitext the first time took 41 s (39
requests); one revision deleted since is asked for again on every run (one request).

### The data actually used

| split | rows | unknown label (left out) | used | news | not news | label sources (used rows) |
|---|---:|---:|---:|---:|---:|---|
| train | 458 | 179 | 279 | 160 | 119 | LLM 231, portal 48 |
| valid | 94 | 52 | 42 | 27 | 15 | LLM 39, portal 3 |
| test | 216 | 84 | 132 | 79 | 53 | LLM 112, portal 20 |

All LLM labels come from `ollama:qwen3:4b` (spec 02b's labeler). No human labels.

### The comparison (acceptance criterion 2)

All four models trained on the real dataset, each a run in the MLflow UI with its
metrics, plots and model (`comparison.md` of the parent run; test split unless named):

| model | test AP | AP 95% interval | gap to best (95%) | ROC AUC | Brier | ECE | threshold | precision | recall | F1 | valid AP | train AP | train s | ms/spike |
|---|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| rule (live default detector) | 0.614 | 0.516 to 0.707 | 0.344 (0.258 to 0.432) | 0.530 | 0.477 | 0.477 | 0.5 (fixed) | 0.629 | 0.494 | 0.553 | 0.616 | 0.584 | 0 | 0.000 |
| logistic regression (C = 0.1) | 0.938 | 0.898 to 0.970 | 0.020 (-0.001 to 0.046) | 0.903 | 0.122 | 0.091 | 0.438 | 0.864 | 0.886 | 0.875 | 0.974 | 0.992 | 1.4 | 0.005 |
| gradient-boosted trees (depth 3, 150 trees) | **0.958** | 0.925 to 0.982 | best | 0.929 | 0.109 | 0.088 | 0.136 | 0.791 | 0.911 | 0.847 | 0.994 | 1.000 | 2.3 | 0.016 |
| neural network + summary embedding (epoch 22) | 0.948 | 0.911 to 0.977 | 0.009 (-0.012 to 0.033) | 0.920 | 0.112 | 0.075 | 0.506 | 0.873 | 0.873 | 0.873 | 0.978 | 0.994 | 0.5 | 0.009 |
| *(ablation) network, features only* | 0.906 | | | 0.872 | 0.141 | 0.095 | | | | | 0.970 | | | |

A random ranking scores 0.598 average precision on this test split (the share of news).
The training times include the hyperparameter search (six fits each for logistic
regression and the trees); "ms/spike" is scoring only, without the network's embedding
(84 ms per spike including the one-off model load).

What the table says:

- **The rule is barely better than chance** at telling news from non-news among
  candidate spikes (AP 0.614 vs. 0.598, ROC AUC 0.53). The live thresholds decide
  *whether something is a spike*, not whether it is news; that is exactly the gap this
  classifier fills.
- **The three learned models are close.** All three intervals overlap widely. The trees
  are best on every threshold-free metric but one, the network is a little better
  calibrated (ECE 0.075), and logistic regression trails the trees by 0.020 in AP, a gap
  whose paired 95 % interval reaches just below 0 (-0.001 to 0.046).
- **The summary embedding helps the network** (0.906 without it, 0.948 with it), and it
  is the network's most important input (shuffling it costs 0.022 AP). But the trees get
  further without any text.
- **Overfitting is visible but harmless here.** The trees reach train AP 1.000 and
  validation 0.994, and their validation-chosen threshold is very low (0.136): on 42
  validation rows nearly every score separated cleanly, so the "most recall at 0.8
  precision" threshold slid down, and on the test split precision dropped to 0.791, just
  under the target. Logistic regression's threshold (0.438) transferred better: test
  precision 0.864 at recall 0.886.

Feature importance (drop in test AP when shuffled, permutation importance):

| rank | logistic regression | gradient-boosted trees | network + embedding |
|---:|---|---|---|
| 1 | `cat_mentions_spike_year` 0.053 | `cat_mentions_spike_year` 0.110 | `summary_embedding` 0.022 |
| 2 | `news_word_share` 0.036 | `news_word_share` 0.039 | `cat_mentions_spike_year` 0.019 |
| 3 | `title_has_year` 0.009 | `page_age_days` 0.007 | `news_word_share` 0.013 |
| 4 | `title_has_spike_year` 0.005 | `minor_share` 0.007 | `title_has_year` 0.005 |
| 5 | `title_words` 0.003 | `anon_share` 0.004 | `cat_deaths_spike_year` 0.005 |

The largest logistic regression weights (on the scaled features) say the same in
readable form: `news_word_share` +0.90, `cat_mentions_spike_year` +0.76, `bytes_added`
+0.40, `cat_living_people` +0.39, `minor_share` -0.38, `revert_share` -0.35,
`title_has_year` +0.28, `vandal_word_share` -0.26.

A warning that goes with these numbers. The two top inputs, "the page has a category
naming the spike's year" (`2026 deaths`, `2026 in sports`, `2026 elections`) and "edit
summaries use news words", are genuine signals known at the cutoff. But 07a already saw
that the LLM labeler never called a page with the year in its title "not news". The
models learn the labeler's view of news, including its shortcuts, and an AP of 0.94 to
0.96 means "agrees with qwen3:4b very well", not "finds news very well". Human labels on
the test days (`uv run python -m wikipulse.eval.label --run-id 07a-candidates --from
2026-11-08 --to 2026-11-09 --manual`) would show how much of it is real.

### The model chosen: logistic regression (`news-classifier` version 2)

`select_model()` registered **logistic regression** as `news-classifier` version 2
(tags `dataset_version=v2`, `dataset_fingerprint=fb6b5a57...`, `dataset_commit=c498214...`,
`model_type=logreg`, `threshold=0.4381`, `training_run=01d64af9...`). Its reason, as
logged: "test average precision 0.938 vs. 0.958 for gbt: the gap's 95% interval [-0.001,
0.046] reaches 0, so not clearly worse, and simpler". Loading
`models:/news-classifier/2` from the server and scoring the 132 test spikes reproduces
AP 0.938 and flags 81 of them.

Why I agree with the rule's choice here, weighing accuracy, simplicity and speed:

- **Accuracy.** The trees are ahead by 0.020 AP on 132 test spikes, and the paired
  bootstrap can't rule out that the difference is luck. At the chosen thresholds,
  logistic regression is actually the better *decision-maker* on test (F1 0.875 vs.
  0.847, precision 0.864 vs. 0.791, against a target of 0.8).
- **Simplicity.** One weight per feature, readable as above; a change in behaviour after
  retraining (07c) can be explained by looking at the weights. It needs no text, no
  embedding model and no extra API request per spike, unlike the network.
- **Speed.** All three score a spike in microseconds, so speed only counts against the
  network (its summary costs an API request and an embedding).
- **Robustness.** With 279 training rows, the model with the fewest ways to overfit is
  the safer bet for next week's spikes (07c's drift monitoring will tell).

The honest caveat: this was decided at the edge. The gap's interval reaches -0.001; a
slightly different test set could have tipped it to the trees, which is what the first
session's fixed-margin rule did (version 1). With more labeled test days the choice
should be revisited, and the trees are the natural challenger for 07c.

### Tests and checks (acceptance criterion 1)

- Training on the tiny synthetic dataset: `tests/ml/test_train.py` (12 tests: all four
  runs logged as children of one parent in the right experiment, metrics computed and
  better than chance, unknown labels left out and counted, the comparison artifacts,
  12 nested candidate runs, the rule's fixed threshold, paired gaps, the selection rule,
  registration with the dataset tags, and loading the registered and every logged model
  to score a table), plus `test_evaluate.py` (8), `test_models.py` (6) and
  `test_summaries.py` (5).
- `uv run pytest`: 941 passed, 1 skipped (before this spec: 910 passed).
- `uv run pytest -m integration`: 67 passed, 1 skipped, including
  `test_training_logs_and_registers_on_the_server` against the running MLflow server.
- `uv run ruff check .` and `uv run ruff format --check .`: clean.
- The server: `docker compose --profile mlflow up -d` builds and starts it; health check
  green; about 430 MB of memory after the training sessions; the UI answers on
  http://localhost:5000; 4 model runs per session found through the REST API; the
  artifacts are in the RustFS bucket `wikipulse-mlflow` (experiment folder `2/`).
- No acceptance step needed scaling down.

## Check your understanding

1. The trees have the highest test average precision, yet logistic regression was
   registered. Under what result would `select_model()` have registered the trees, and
   why is a *paired* bootstrap the right tool for that decision rather than comparing
   the two models' separate intervals?
2. Each model's threshold was chosen on the validation split for "precision at least
   0.8". Why not choose it on the test split, and what went wrong with the trees'
   threshold (0.136) on the test split?
3. The neural network with the summary embedding beats its features-only twin by 0.04
   AP. Why does `summaries.py` fetch an old revision's wikitext instead of using the
   page summaries stage 3 already stores, and what would the numbers probably have done
   with stage 3's summaries?

<details>
<summary>Answers</summary>

1. If the 95 % interval of AP(trees) - AP(logistic regression) had stayed above 0 (its
   lower end was -0.001), logistic regression would not have qualified. The next model
   in order of simplicity is the trees, the best model, which always qualifies, so the
   trees would have been registered (the rule never qualifies: its gap's interval is
   0.258 to 0.432). A paired bootstrap resamples the *same* rows for both models, so
   the luck of the sample (a few hard spikes in or out) moves both scores together and
   cancels in the difference. Two separate intervals each contain that sample luck, so
   they overlap even when one model is consistently better on the same rows.
2. Choosing it on the test split would make the test numbers part of the training: the
   reported precision would be the target by construction, not a measurement of how the
   threshold works on new spikes. The trees separated the 42 validation rows almost
   perfectly (validation AP 0.994), so the threshold reaching 0.8 precision with the most
   recall slid all the way down to 0.136. Such a low threshold relies on the model's
   scores for borderline spikes, which are overconfident after overfitting; on the test
   split more non-news spikes scored above 0.136, and precision fell to 0.791.
3. Stage 3 stores the summary as it was when the page was enriched, which can be hours
   or days after the cutoff, and a news event changes the article's opening sentence
   ("... was an American singer" after a death, "... is the president-elect" after an
   election). The embedding of that text would carry the answer: leakage. With today's
   summaries the network (and its ablation gap) would probably have looked better on
   this test set and then disappointed live, where only the text at the cutoff exists.
   Fetching the revision current at the cutoff (`page_revision_id`) gives the text a live
   model would really see.

</details>

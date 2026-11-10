# Review: Spec 07c, Model serving, monitoring and retraining

Branch `spec-07c-serving-and-retraining` (9 commits, 34 files) compared with `main`.

## Acceptance criteria

| Criterion | Result | Evidence |
|---|---|---|
| Tests for loading, scoring, the promotion rule and drift calculation (synthetic data) | pass | `tests/ml/test_serve.py` (loading by alias, holder reload, scoring, edit buffer, live scorer), `tests/jobs/test_retrain.py` (`decide()`, `compare_models()`), `tests/ml/test_drift.py` (PSI, KS, report), `tests/ml/test_registry.py` (promote, rollback). All use synthetic data and a throwaway SQLite MLflow. `uv run pytest`: 992 passed, 1 skipped (Airflow import test, which only runs in the Airflow image), 75 deselected. `uv run pytest -m integration tests/db/test_scores_db.py tests/dags`: 4 passed. `tests/dags` inside the running Airflow container: 33 passed. `ruff check` and `ruff format --check` are clean. |
| One full retraining cycle in Airflow, ending in a promotion or a documented rejection | pass | `airflow dags list-runs retrain`: `backfill__2026-11-09T00:00:00+00:00` is `success`, and all 5 tasks are `success` (`check_drift` passed on attempt 2, after the MLflow Host-header fix). The challenger v4 is tagged `status=rejected`, `decision=reject`, `rejected_reason="... gap +0.000 ... below the margin 0.01"`, `champion_at_evaluation=3`. `registry show`: v3 is `@champion` and v4 is `@challenger`/rejected. The train log shows that all four models were trained (rule 0.614, logreg 0.938, gbt 0.958, torch 0.948) and logreg was picked. `drift_reports` holds 2 rows (02b-baseline `ok`, 07a-default `drift`). The rejection is documented in the learning note. |

Other checks: no dependency changes in `pyproject.toml` or `uv.lock`, nothing under `data/`, no `.env`, `devlog.md` untouched, the new settings are in `config.py` and `.env.example`, the spec status is `done`, and the learning note exists. The removed DAG tests were extended rather than weakened (`test_both_dags_exist` became `test_all_dags_exist`, and the parametrisation gained `retrain`).

## Problems

1. **medium**, `wikipulse/ml/serve.py:253` (and `model_inputs`/`score_spikes` in general): **training/serving skew.** It goes beyond "threshold 4 vs 5". Training rows are *candidate* spikes, with their feature window and cutoff anchored at the candidate's `started_at`. Serving scores *default-detector* spikes, anchored at a later `started_at`, so every window-based feature is measured at a different moment. I confirmed it in dataset v2: 46 of the 152 default-rule reference rows have `edits_before_start == 4`, a value serving can never produce. This was inherited from 07a/07b and is documented, but the scores shown in the feed come from a distribution the model wasn't trained on, and nothing tracks a fix: there is no roadmap entry or follow-up spec, only the learning note.
2. **medium**, `wikipulse/ml/drift.py:191-194`, `wikipulse/config.py` (`ml_drift_min_rows=30`, `ml_drift_psi=0.2`): **the default drift thresholds sit below PSI's noise floor.** I ran `psi()` 2,000 times on two samples from the same Beta(2,2) distribution (152 reference rows). The score alone crosses 0.2 in 88% of runs at n=30, 66% at n=50, 24% at n=100 and 5% at n=200. With these defaults, a status of `drift` (decided by the score alone) is mostly a false alarm for any week below about 200 scored spikes, and stage 9 plans to alert on it.
3. **medium**, `wikipulse/jobs/retrain.py:317-343`: **the promotion test set is the same one the challenger was selected on.** `train_all` picks among four models by *test* AP (`choose()` in `train.py`, `simplest_not_worse`/`best_test_ap`). `step_evaluate` then compares challenger and champion on that same test split. The challenger's AP is optimistically biased (winner's curse), and with four candidates that bias can exceed the 0.01 margin.
4. **medium**, `wikipulse/jobs/retrain.py:168-204` and `:388`: **re-running `train_challenger` after a promotion corrupts the champion's tags.** `existing_challenger()` returns the version registered for the same week and dataset even when it is now `@champion`. `step_train` then sets `@challenger` and `status=challenger` on it, `evaluate` rejects it ("the challenger is the champion itself"), and `promote` tags the live champion `status=rejected`. This contradicts the note's claim that retries are safe.
5. **medium**, `dags/retrain.py:57` / `ML_LAKE_PREFIX=history/edits`: **the weekly DAG retrains on a lake prefix that never grows.** Every run rebuilds the same data (v3 has the same fingerprint as v2), trains the same model and rejects it, so the goal "retrain it on a schedule" is wired but not working. The DAG is also paused. The acceptance criterion is still met, because a documented rejection is allowed. This needs a tracked follow-up (default to the live `edits` prefix, or combine both).
6. **low**, `wikipulse/ml/serve.py:154-156, 469-473`: `ModelHolder.current()` catches only resolve errors. If the alias moves and the artifact download fails (S3/RustFS down), or a revision or DB call in `score_due` fails after its retries, the live scorer crashes instead of "keeping serving". A restart recovers, because the scorer re-reads 26 h of the stream.
7. **low**, `wikipulse/jobs/retrain.py:170`: `ml_retrain_models.split(",")` is neither stripped nor validated. `"logreg, gbt"` silently drops `gbt`, and an all-unknown value trains nothing without an error.
8. **low**, `wikipulse/ml/serve.py:253` plus the data in `spike_scores`: `default_rule_fired` is hard-coded to True. The note itself says a loose run would need it computed, yet 89 spikes of `02b-loose` were backfilled for the feed demo with that wrong input.
9. **low**, `wikipulse/ml/registry.py:119-121`: `rollback_target()` follows `previous_champion` without checking the target's `status`. The `NOT_FOR_ROLLBACK` guard applies only to the fallback search.
10. **low**, `docker/airflow/Dockerfile:19-25`: the Airflow image grew from 3.3 GB to 6.0 GB for one weekly DAG. The CI `dags` job now builds it every run. A separate ML image, or a CI build that skips the ML venv, would keep CI fast. Acceptable for now.
11. **low**, `wikipulse/ml/drift.py:52-53`: `BINS` and `EPSILON` are module constants. `EPSILON=1e-4` per empty bin is what inflates PSI at small n (problem 2), so it arguably belongs in config next to `ML_DRIFT_PSI`.
12. **low**, `wikipulse/jobs/retrain.py:356-392`: `step_promote` (the stale-champion guard, the reject path, and idempotence after a promotion) has no unit test. Only `decide()` and `compare_models()` are tested.
13. **low**, `wikipulse/events/feed.py:77` and `wikipulse/api/events.py:113`: `news_score()` and `best_news_score()` duplicate the same logic.

### The implementer's flagged items

- **Skew (4 vs 5):** acceptable to merge as a documented, inherited issue. Drift monitoring caught it, which is the point of drift monitoring. But it is broader than one threshold (problem 1) and needs a tracked follow-up.
- **Same history every week:** acceptable for the acceptance criterion, because a laptop has limited data. Not acceptable as the steady state (problem 5).
- **DAG paused:** acceptable and consistent with 06c, as long as problem 5 is tracked.
- **Noisy PSI at small n:** not acceptable with the current defaults. The noise is worse than the note says (problem 2), and a config change fixes it cheaply.
- **6 GB image:** acceptable (problem 10).

Verdict: fix

The required fixes are small: change the drift defaults or the noise handling (2), stop `train` and `promote` from touching a version that is already champion (4), and track 1 and 5 as follow-ups. Item 3 should at least be documented, or fixed by selecting on valid.

## Questions for the human before merging

1. The live scorer scores spikes of the *default* detector, while the model was trained on *candidate* spikes. For a single page burst, which moment anchors the feature window in training and which in serving? Why does that make more than one feature differ, and what would you change to remove the skew?
2. Suppose a week has 40 scored spikes and the drift report says `drift` because the score's PSI is 0.31. How much should you trust that, what in `psi()` and the defaults makes small samples look drifted, and what would you change before stage 9 turns this into an alert?
3. The challenger is chosen among four models by test AP and then compared with the champion on that same test split. Why does that favour the challenger, why does using the *same rows* for both models not remove the bias, and which split should the selection use instead?

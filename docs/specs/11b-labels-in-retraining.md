# Spec 11b: Fresh labels for weekly retraining

Status: done
Stage: 11. Model quality
Owner: agent
Depends on: 11a

## Goal
The weekly retraining DAG labels its new spikes itself and builds its test split only
from days whose labels are final, so a run is never rejected just because the newest
data has no labels yet.

## Context
- `dags/retrain.py` and `wikipulse/jobs/retrain.py` (07c): `check_drift` →
  `extend_history` → `build_dataset` → `train_challenger` → `evaluate_against_champion`
  → `promote`. `docs/ROADMAP.md` ("Follow-ups") describes the gap: in dataset v4, 105 of
  173 test rows had no label.
- Labels come from `wikipulse.eval.label` through `wikipulse.llm`. Human labels
  (`labeler: "human"`) always win and are never overwritten.
- The Wikipedia "Current events" portal for a day keeps changing for a few days; 07a
  already has the idea of a portal day being *final*.

## Inputs
- New spikes from the dataset mode of spec 11a, `evals/labels/spike_labels.jsonl`, the
  portal ground truth, Ollama.

## Outputs
- A `label_new_spikes` task in the retraining DAG, before `build_dataset`, that labels
  unlabeled spikes of the new period with the LLM labeler, within a budget
  (`ML_RETRAIN_LABEL_BUDGET`, a number of spikes; `.env.example` documents it).
- A label delay: the test split uses only days whose portal is final and whose spikes
  are labeled (`ML_LABEL_DELAY_DAYS`); newer days go to training or wait.
- Metrics or log lines for labels added, budget used and rows still unlabeled.
- Tests, and `docs/learning/11b-labels-in-retraining.md`.

## Requirements
1. Labeling is idempotent: re-running the task labels nothing twice and never touches a
   human label.
2. If Ollama is down or the budget runs out, the task logs it and the DAG continues with
   the labels it has; it never fails the whole run for missing labels alone.
3. The rejection reason "too few labeled test rows" can only happen when even the
   label-delayed test split is too small, and the message says how many rows were
   labeled, unlabeled and delayed.
4. Unit tests run without network or LLM (a fake labeler through the existing test
   helpers).

## Acceptance criteria
- Unit tests for idempotence, budget, the Ollama-down path and the label delay pass.
- One full retraining cycle in Airflow on a period with unlabeled spikes: the new task
  labels them, the test split respects the delay, and the run ends in a promotion or a
  documented rejection that isn't caused by missing labels.
- `uv run pytest`, `ruff check` and `ruff format --check` pass.

## What you'll learn
- **Label latency:** why ground truth arrives late in real systems, and how pipelines
  wait for it.
- **Budgets in automated labeling:** bounding cost and time per run.

## Look at it yourself (optional, never blocking)
- Trigger the DAG by hand and watch the labeling task's logs in Airflow.

## Out of scope
- Changing the labeler's prompt or model.
- Active learning or choosing which spikes to label by uncertainty (a good later spec).

## Notes for the agent
- Keep the DAG paused at the end, as 06c and 07c left it.
- Update the matching follow-up in `docs/ROADMAP.md`.

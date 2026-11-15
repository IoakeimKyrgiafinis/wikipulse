# Spec 11a: Fix the news classifier's training/serving skew

Status: ready
Stage: 11. Model quality
Owner: agent
Depends on: 07c

## Goal
The news classifier is trained on rows built exactly the way `wikipulse.ml.serve` scores
spikes live, so its live scores come from the distribution it learned. A new dataset
version and a retrained challenger are compared with the champion, and drift is re-checked.

## Context
- The 07c review found the skew; `docs/ROADMAP.md` ("Follow-ups") describes it in full.
  Training rows (`wikipulse/ml/dataset.py`, datasets v2 to v4) are spikes of the looser
  *candidate* detector, with the feature window and cutoff anchored at the candidate's
  start. Serving (`wikipulse/ml/serve.py`) scores spikes of the *default* detector, which
  start later. Every window-based feature is measured at a different moment, and values
  such as `edits_before_start == 4` (46 of 152 default-rule rows in v2) never occur when
  serving. Drift monitoring (`wikipulse/ml/drift.py`) flags `edits_before_start` every week.
- Read the learning notes for 07a, 07b and 07c first: they explain the as-of labels, the
  purged splits, the selection rule and the promotion margin. Keep all of them.
- Labels come from `wikipulse.eval.label` (LLM) and, more and more, from the human
  (`labeler: "human"`, which always wins). Never overwrite a human label.

## Inputs
- The history lake (`history/edits`), the default detector settings, the portal ground
  truth and `evals/labels/spike_labels.jsonl`.
- The MLflow registry (`news-classifier`, `@champion`).

## Outputs
- `wikipulse/ml/dataset.py`: a dataset mode (or a new builder) whose rows are the
  default detector's spikes, with the feature window and cutoff computed by the same
  function `serve.py` uses. Share that function; don't copy it.
- A new dataset version in the lake, with its manifest stating the mode.
- LLM labels for the new rows that lack one, within a stated budget.
- A retrained challenger registered in MLflow and compared with the champion by the
  existing promotion rule; promoted or rejected with the reason recorded.
- A drift report comparing the new training data with recent live scores.
- `docs/learning/11a-classifier-skew-fix.md`.

## Requirements
1. Training and serving compute features through one shared code path. A test builds a
   row with the dataset code and with the serving code for the same spike and the same
   edits, and asserts the features are identical.
2. No training row can have a feature value that serving can't produce for the same
   detector settings (for example `edits_before_start` below the default threshold). A
   test checks this on a synthetic day.
3. Older dataset versions stay readable and untouched; the new version is additive.
4. Splits stay purged and time-ordered; the test split is the newest days whose portal
   is final.
5. The comparison with the champion uses the same test rows for both models. If the old
   champion can't be evaluated on the new rows, say why and use the closest fair setup.
6. Event time only: nothing in the dataset build reads the wall clock except for
   "as of" bookkeeping already in place.

## Acceptance criteria
- The shared-features test and the impossible-values test pass.
- A new dataset version exists, with row counts, label coverage and class balance
  reported in the learning note.
- A challenger is trained, compared on identical test rows, and promoted or rejected,
  with the numbers in the learning note.
- A drift report on the new reference data shows whether `edits_before_start` still
  drifts; the result is reported, whatever it is.
- `uv run pytest`, `ruff check` and `ruff format --check` pass.

## What you'll learn
- **Training/serving skew:** the most common silent failure in production ML, and why
  "share the feature code" is the standard fix (feature stores exist for this).
- **Label budgets:** what it costs to label a new dataset and how to prioritise.
- **Honest comparisons** when the data itself changes between model versions.

## Look at it yourself (optional, never blocking)
- Pick five spikes and compare their old and new feature rows side by side.
- Read the drift report before and after.

## Out of scope
- Changing the detector itself.
- New features or new model families.
- The labeling task inside the weekly DAG (spec 11b).

## Notes for the agent
- If the default detector yields too few positive rows for a sensible split, say so,
  report the numbers, and consider scoring candidate-detector spikes live instead (the
  other fix the follow-up names). Record the choice and the reason.
- Remove the follow-up from `docs/ROADMAP.md` only if it is fully fixed; otherwise update it.

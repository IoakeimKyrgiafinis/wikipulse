# Spec 12b: Audit the LLM labelers and judges against human labels

Status: ready
Stage: 12. AI quality
Owner: agent
Depends on: human labels (see Context)

## Goal
Every LLM labeler and judge in the project gets a measured agreement with human labels,
every headline number in the README gets a human-checked version where enough human labels
exist, and the CI gate's labels are rebuilt from human judgement.

## Context
- Until now every label is an LLM label: spikes (02b, 07a), page pairs (03b), relations
  (04c), QA judgements (05b) and the CI gate labels (09c). The human is now labeling by
  hand with the existing `--manual` tools; human records have `labeler: "human"` and always
  win. Never overwrite or edit a human label.
- **Precondition:** at least 50 human spike labels on one 02b run. If there are fewer,
  set this spec to `blocked` with the current counts and stop.
- An optional stronger judge is possible through the `anthropic:` backend of
  `wikipulse.llm`, but it costs money and sends text to an external API. Use it only if
  `ANTHROPIC_API_KEY` is set **and** the human set `JUDGE_AUDIT_ALLOW_PAID=true` in `.env`.
  Otherwise skip that part and say so.

## Inputs
- All files in `evals/labels/`, the eval reports in `evals/results/`, the gate files.

## Outputs
- `wikipulse/eval/audit.py` (`uv run python -m wikipulse.eval.audit`): for each label
  family, counts of human labels, the LLM's agreement with them (accuracy, Cohen's kappa,
  confusion matrix), with bootstrap intervals, written to `evals/results/<date>_audit.md`
  and `.json`.
- Re-computed headline metrics using human labels where they exist (detection precision
  per 02b run, grouping, relations, QA), side by side with the LLM-label numbers.
- The CI gate labels rebuilt with `scripts/make_gate_fixture.py` (which already prefers
  human labels), and a new baseline written with `--write-baseline`, with the old and new
  numbers in the learning note.
- README "Results" updated to show human-checked numbers next to the LLM ones, with
  sample sizes.
- Tests, and `docs/learning/12b-judge-audit.md`.

## Requirements
1. Report sample sizes and intervals everywhere; say clearly where human labels are too
   few to conclude anything.
2. Treat `unsure` explicitly (excluded and counted), the same way for human and LLM.
3. The audit is read-only on label files; it never writes a label.
4. The gate baseline change is explained and flagged for the human's review (CODEOWNERS).

## Acceptance criteria
- The audit report exists and covers every label family, with counts even where they are zero.
- The gate passes on the rebuilt labels and new baseline; CI is green.
- README results show both numbers with their sources.
- `uv run pytest`, `ruff check` and `ruff format --check` pass.

## What you'll learn
- **Measuring your measuring instrument:** inter-rater agreement, Cohen's kappa, and why
  an LLM judge needs calibration before you trust its numbers.
- **Reporting with small samples:** intervals, and saying "we don't know yet".

## Look at it yourself (optional, never blocking)
- Read the spikes where you and the LLM disagreed most. Who was right?

## Out of scope
- Writing labels for the human.
- Changing the labelers' prompts (a follow-up if the audit shows a systematic error).

## Notes for the agent
- Run it again after each labeling session; it should be cheap and fast.

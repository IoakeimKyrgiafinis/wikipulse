# Spec 11c: A vandalism signal for spike detection

Status: ready
Stage: 11. Model quality
Owner: agent
Depends on: 11a

## Goal
Bursts that are mostly vandalism and reverts stop showing up as news. A measured
vandalism signal is added where it helps most (detector, classifier or both), and
precision improves without losing recall beyond a stated margin.

## Context
- In one labeled sample, 21 of 26 "not news" spikes were vandalism bursts (see the 02b
  and 07b learning notes). The detector already recognises reverts
  (`wikipulse/detect/detector.py`, `is_revert`, `revert_edits`, `count_reverts`).
- The CI eval gate (`wikipulse/eval/gate.py`) holds detection precision and recall on a
  committed recording; its fixture keeps a revert flag per edit.
- The classifier (07b, retrained in 11a) already has 43 features; check which describe
  reverts before adding new ones.

## Inputs
- Recordings and the history lake, `evals/labels/spike_labels.jsonl` (human labels first),
  the gate fixture and baseline, the dataset from 11a.

## Outputs
- An analysis (`evals/results/<date>_vandalism.md`): how revert share, revert speed,
  anonymous-editor share (as a count, never an identity) and similar signals separate
  news from not-news spikes, with the labels used and their labelers.
- The chosen change, for example a detector rule ("a spike whose edits are mostly
  reverted within N minutes doesn't start or is marked `vandalism`") and/or new
  classifier features, with settings in `config.py` and `.env.example`.
- An updated gate baseline if detection changes, through `--write-baseline`, with the
  before and after numbers in the learning note.
- Tests, and `docs/learning/11c-vandalism-signal.md`.

## Requirements
1. Decide from the analysis, not in advance: report the signals that don't help too.
2. Prefer human labels; report results separately for human and LLM labels when both exist.
3. No personal data: use counts and shares of anonymous editors, never IPs or usernames.
4. Event time only; replays must stay deterministic (the 02a determinism check passes).
5. Recall may drop by at most one news burst on the gate fixture unless the learning
   note argues for more.

## Acceptance criteria
- The analysis report exists, with sample sizes and margins.
- On the gate fixture, precision rises and recall stays within the margin; the gate
  passes with the new baseline, and the baseline change is explained.
- If the classifier changes, a retrained challenger is compared with the champion by the
  existing rule.
- The determinism check, `uv run pytest`, `ruff check` and `ruff format --check` pass.

## What you'll learn
- **Feature analysis before modelling:** looking at the data before choosing a rule.
- **Precision/recall trade-offs** and how a CI gate keeps you honest about them.

## Look at it yourself (optional, never blocking)
- Read ten bursts the new rule drops and decide whether you agree.

## Out of scope
- A separate vandalism classifier service.
- Reverting or reporting vandalism on Wikipedia.

## Notes for the agent
- The baseline lives in `evals/baselines/detection_gate.json` and is covered by CODEOWNERS:
  say clearly in the summary that the human must review the baseline change.

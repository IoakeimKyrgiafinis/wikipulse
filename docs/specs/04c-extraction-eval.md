# Spec 04c: Extraction evaluation

Status: done
Stage: 4. Knowledge graph
Owner: agent
Depends on: 04b

## Goal
Measure the quality of the knowledge graph, using Wikidata's own facts and your labels as ground truth.

## What you'll learn
- **Evaluating LLM extraction:** entity precision and recall, relation accuracy, linking accuracy, each telling you about a different failure.
- **Using a public knowledge base as ground truth,** and its blind spots (Wikidata lags behind breaking news).
- **Prompt iteration with evidence:** change the prompt, re-run, compare. Never "it seems better."

## Outputs
- `wikipulse/eval/extraction.py`: `--events <ids or date range> --name <run>`; reports in `evals/results/`
- A labeling step where an LLM judges a sample of 50 extracted relationships against their source text as correct, wrong or unsure, saved to `evals/labels/relation_labels.jsonl` (labeler recorded; `--manual` for a human)

## Requirements
1. **Linking accuracy:** for entities whose names exactly match a Wikidata label, does the linked ID match?
2. **Relation support:** the share of extracted relationships between linked entities that Wikidata also states (any matching property). Report it as a lower bound, since news relationships are often too new for Wikidata.
3. **Labeled precision** on the labeled sample, stating who labeled it.
4. Report per entity type, with the prompt version and model in the config snapshot.

## Acceptance criteria
- Unit tests with saved Wikidata responses.
- A run over one day; report the numbers.

## Look at it yourself (optional, never blocking)
- Label 50 relationships, then try one prompt change and compare the two runs.

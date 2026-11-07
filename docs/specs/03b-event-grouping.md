# Spec 03b: Event grouping

Status: done
Stage: 3. Event grouping
Owner: agent
Depends on: 03a

## Goal
Group spikes that belong to the same story into one event, for example the election, the winning candidate and the country's page, so the system reports stories rather than loose pages.

## What you'll learn
- **Clustering in practice:** combining several similarity signals (links, timing, meaning) into one decision. Read a short introduction to connected components and to cosine similarity first.
- **Embeddings as a general tool,** used here for clustering rather than RAG.
- **Evaluating clustering** with pairwise precision and recall, and why clustering is notoriously hard to evaluate.
- **Online algorithms:** events grow as new spikes arrive, unlike clustering a fixed dataset.

## Context
- Signals that two spiking pages belong together: one links to the other; their spikes started close in time; their summaries mean similar things.
- Use a small local embedding model (`BAAI/bge-small-en-v1.5` via `sentence-transformers`) on page summaries.

## Outputs
- `wikipulse/events/similarity.py`: a pairwise score combining links, time overlap and embedding similarity, with weights in config
- `wikipulse/events/grouper.py`: assigns each new spike to an existing active event or starts a new one; merges events when they turn out to be one story
- Migration adding `events` and `event_pages` tables
- `uv run python -m wikipulse.events.run` and `uv run python -m wikipulse.events.feed` (prints active events with their pages)
- `wikipulse/eval/grouping.py`: pairwise precision and recall against labeled groupings in `evals/labels/grouping_labels.jsonl`, plus a labeling step where an LLM judges sampled page pairs as same story or not (labeler recorded; `--manual` for a human)

## Requirements
1. Event assignment is deterministic for the same input order.
2. An event ends when all its spikes have ended.
3. Each event gets a provisional title (its highest-scoring page) until stage 4 adds better ones.

## Acceptance criteria
- Unit tests for the similarity score, assignment, merging and the metrics.
- A replay of a recorded day produces an event feed; report the number of events and pages per event.

## Look at it yourself (optional, never blocking)
- Label groupings for one day with the labeling tool, then run the evaluation. Tune the weights once, and compare.

## Out of scope
- LLM-generated event titles (stage 4)

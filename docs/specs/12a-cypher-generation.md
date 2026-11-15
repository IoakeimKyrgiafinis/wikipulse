# Spec 12a: Better Cypher generation for question answering

Status: ready
Stage: 12. AI quality
Owner: agent
Depends on: 05b

## Goal
Fewer questions fail because the LLM-written Cypher query never runs or returns the wrong
rows. Measured on the frozen 05b question set, correctness rises without hurting
faithfulness.

## Context
- GraphRAG (`wikipulse/qa/`) has three channels; one is a single read-only Cypher query
  written by the LLM and checked by `wikipulse/qa/cypher.py` before it runs.
- The 05b evaluation found four failures that were Cypher queries that never ran, traced
  to the prompt (date arithmetic, exact name matching) and to a false positive in the
  validator. Read `docs/learning/05b-qa-evals.md` and its results in `evals/results/`.
- The eval runs on a frozen copy of the graph (`neo4j-eval`, port 7688, compose profile
  `eval`), so before/after numbers are comparable.

## Inputs
- `evals/labels/qa_questions.jsonl`, the frozen eval graph, the 05b judge setup.

## Outputs
- A fixed validator (the false positive gets a regression test, and every existing
  safety test still passes).
- An improved Cypher prompt in `wikipulse/qa/prompts/`, as a new version (keep the old one
  selectable), with few-shot examples for dates and exact name matching taken from the
  schema, not from the eval questions.
- Optionally, one repair attempt: when a query fails validation or errors, the LLM gets
  the error and one chance to fix it (bounded, logged).
- A before/after evaluation report in `evals/results/`, and
  `docs/learning/12a-cypher-generation.md`.

## Requirements
1. The validator stays strict: read-only, bounded hops and rows. No safety test is
   weakened or removed.
2. Few-shot examples must not leak the eval answers; a test checks that no eval question
   or expected answer text appears in the prompt.
3. Report correctness, fully-correct share, faithfulness, entity recall, correct refusals,
   the number of Cypher queries that failed to run, and latency, before and after, with
   the 05b margins.
4. All LLM calls through `wikipulse.llm`.

## Acceptance criteria
- The validator regression test passes, and so do all existing `tests/qa` tests.
- The eval runs on the frozen graph with the new prompt; the report shows fewer failed
  queries. If correctness doesn't improve beyond noise, the note says so honestly and
  explains why.
- `uv run pytest`, `ruff check` and `ruff format --check` pass.

## What you'll learn
- **Text-to-query generation:** why schema-grounded few-shot examples and error feedback
  help small models.
- **Prompt versioning and eval-driven iteration** without overfitting to the eval set.

## Look at it yourself (optional, never blocking)
- Ask the web page or `wikipulse.qa.ask` the four questions that failed before.

## Out of scope
- Changing the retrieval channels' design or the graph schema.
- A bigger answering model.

## Notes for the agent
- Start the eval graph with `docker compose --profile eval up -d`.
- If human QA judgements exist by then (`labeler: "human"`), report against them too.

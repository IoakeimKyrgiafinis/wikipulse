# Spec 05b: Question-answering evals

Status: done
Stage: 5. GraphRAG, MCP, agent
Owner: agent
Depends on: 05a

## Goal
Score the question answering on a fixed set of questions, so every retrieval change is judged by numbers.

## What you'll learn
- **Evals for a changing world:** live data means answers change. Freezing a graph snapshot makes evals repeatable. Explain why in your devlog.
- **LLM-as-judge and its calibration:** compare the judge with your own ratings.
- **Retrieval vs generation failures:** separate scores tell you which half to fix.

## Context
- Evals run against a **frozen snapshot**: a Neo4j dump of a chosen day, restored into a separate database, so questions keep the same answers.
- Generate 25 questions from the frozen snapshot into `evals/labels/qa_questions.jsonl`: `id`, `question`, `type`, `expected_answer`, `expected_entities` (Wikidata IDs), `answerable`, `source: synthetic`. Derive expected answers from the graph itself with deterministic queries where possible, cover all question types from spec 05a, and include unanswerable ones.

## Outputs
- `scripts/snapshot_graph.py` (dump and restore)
- `wikipulse/eval/qa.py`: `--name <run>`; two passes (answer everything, then judge everything) so models aren't swapped per question on one GPU
- Judge prompts in `wikipulse/eval/prompts/`
- Metrics: entity recall (did retrieval find the expected entities?), answer correctness (judge, 0–2), faithfulness (claims supported by retrieved facts), correct refusals, latency; overall and per type

## Acceptance criteria
- Unit tests with mocked LLM for metrics and judge parsing.
- A run on the generated questions completes; report the numbers.
- Judge reliability: run two different judge models on the same answers and report their agreement.

## Look at it yourself (optional, never blocking)
- Write 25 questions, rate 15 answers yourself and compare with the judge. Record the agreement.

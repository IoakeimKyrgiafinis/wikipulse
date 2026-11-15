# Spec 12c: Fine-tune a small extraction model

Status: ready
Stage: 12. AI quality
Owner: agent
Depends on: 04c, 12b (for the human-checked relation labels, if available)

## Goal
A smaller model, fine-tuned on the project's own extractions, extracts entities and
relations about as well as qwen3:4b, faster and with less memory, measured with the 04c
evaluation. If it doesn't, the learning note shows why.

## Context
- Graph extraction (`wikipulse/graph/`, 04b) uses qwen3:4b through Ollama with a JSON
  schema; 04c measured it: Wikidata linking 95.9%, relations Wikidata also has 31.9%,
  labeled relation precision 93.9% (about 80% by hand). Read both learning notes.
- Training data: the stored extractions (`graph_extractions`, Neo4j) with their source
  texts, filtered by the relation labels (prefer human ones, then the LLM judge's
  `correct`). Keep a held-out set of events by **day**, never by random rows.
- The laptop has a GPU of limited memory (16 GB system RAM). Pick the base model and
  method to fit: for example LoRA on a 0.6B to 1.7B model. Check the GPU and VRAM first and
  record them. If no usable GPU is available, stop after preparing the data and set the
  spec to `blocked` with the reason, rather than training on CPU for days.

## Inputs
- Extractions and source texts from Postgres and Neo4j, `evals/labels/relation_labels.jsonl`,
  the 04c evaluation code.

## Outputs
- `wikipulse/graph/finetune/`: data preparation (instruction/JSON pairs), training
  script, and export of the result to a format Ollama can serve (GGUF plus a Modelfile),
  or another local runtime reached through a new `wikipulse.llm` backend.
- New optional dependencies (for example `transformers`, `peft`, `datasets`) in a
  separate `pyproject.toml` extra, so the main install stays small; explain each.
- An evaluation report comparing the fine-tuned model with qwen3:4b on the held-out days:
  linking accuracy, Wikidata-confirmed relations, judged precision, JSON validity rate,
  latency per event and memory.
- The model registered in MLflow (parameters, data version, metrics); the weights stay
  out of git (`data/` or the MLflow artifact store).
- `docs/learning/12c-finetuned-extractor.md`.

## Requirements
1. No leakage: held-out days are never in the training data; a test checks the split.
2. Training is reproducible: fixed seeds, the data version and the base model recorded.
3. The extractor can switch models by setting (`EXTRACT_MODEL`) with no code change.
4. Long jobs run in the background; if training would exceed 60 minutes, use a smaller
   sample or model and record it (AGENTS.md).

## Acceptance criteria
- Data preparation tests (format, split, no leakage) pass.
- A fine-tuned model is trained, served locally, and evaluated on the held-out days, with
  all metrics above in the report, whatever the outcome.
- `uv run pytest`, `ruff check` and `ruff format --check` pass.

## What you'll learn
- **Fine-tuning vs prompting:** when a small specialised model can replace a bigger
  general one, and what it costs.
- **LoRA, quantisation and serving** a custom model locally.
- **Distillation:** learning from a bigger model's outputs, and its limits.

## Look at it yourself (optional, never blocking)
- Run both models on the same five events and compare the JSON by eye.

## Out of scope
- Cloud GPUs or paid training services.
- Replacing the production extractor without a better measured result.

## Notes for the agent
- Don't commit model weights or training data dumps.
- If the result is worse, keep qwen3:4b as the default and say so plainly.

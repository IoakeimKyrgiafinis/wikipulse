# Spec 04b: Knowledge graph extraction

Status: done
Stage: 4. Knowledge graph
Owner: agent
Depends on: 03b, 04a

## Goal
For each event, extract the people, organizations, places and their relationships with the LLM, link them to Wikidata, and store them in Neo4j with full provenance.

## What you'll learn
- **Information extraction with LLMs:** prompt design for structured output, and why schemas constrain errors.
- **Knowledge graph modeling:** what are nodes, what are relationships, what are properties. Sketch your graph on paper before the agent runs and compare.
- **Entity resolution,** the hardest problem in knowledge graphs, and how stable IDs (Wikidata) solve much of it.
- **Neo4j and Cypher:** run queries yourself in the Neo4j Browser; the free online "Cypher in 20 minutes"-style tutorials are enough.
- **Cost-aware architecture:** the LLM only runs on events, never on the raw stream.

## Context
- Inputs per event: page summaries and the text of recent changes on its pages (fetched from the API, trimmed).
- Graph model (revise before running): nodes `Entity` (with `wikidata_id`, `name`, `type`: person, organization, place, event, other) and `Event`; relationships `INVOLVED_IN` (entity to event) and `RELATED_TO` (entity to entity, with a `relation` label such as "leader of" or "member of"). Every relationship stores `source_title`, `source_revision`, `extracted_at`, `confidence`.
- Entity linking: match extracted names to Wikidata IDs, first using the IDs of the event's own pages and their links (from stage 3), then a Wikidata search for the rest. Unlinked entities are kept, marked as unlinked.

## Outputs
- `docker-compose.yml`: Neo4j Community added, with a volume and health check
- `wikipulse/graph/extract.py`, `wikipulse/graph/link.py`, `wikipulse/graph/store.py`
- `wikipulse/graph/prompts/extract_v1.md`
- `uv run python -m wikipulse.graph.run`: processes events as they appear
- LLM-generated event titles and one-sentence descriptions, stored on `Event` nodes
- Config: `EXTRACT_MODEL` (default `ollama:qwen3:4b`)

## Requirements
1. Writes are idempotent: re-processing an event updates rather than duplicates.
2. One entity per Wikidata ID, however it was named.
3. Extraction failures are logged per event and retried later.

## Acceptance criteria
- Unit tests with a mocked LLM: parsing, linking order, idempotent writes (against a test Neo4j, `integration`).
- A replayed day produces a graph; report events processed, entities, linked share and average time per event.

## Look at it yourself (optional, never blocking)
- Explore one event in Neo4j Browser. Is the graph right? Note errors in the devlog for spec 04c.

## Out of scope
- Question answering (stage 5)

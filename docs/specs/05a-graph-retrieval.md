# Spec 05a: GraphRAG retrieval and answering

Status: ready
Stage: 5. GraphRAG, MCP, agent
Owner: agent
Depends on: 04b

## Goal
Answer questions about current events from the knowledge graph and page text, with citations. This is the heart of GraphRAG, so explain the design in the learning note.

## What you'll learn
- **How GraphRAG differs from plain RAG:** questions like "how are X and Y connected?" have answers spread across many facts, not inside one chunk. Read one or two short articles on GraphRAG first.
- **Text-to-query:** the LLM writing Cypher, and why you validate and limit generated queries before running them.
- **Hybrid retrieval:** combining graph traversal with vector search (Neo4j has a built-in vector index).
- **Grounded generation:** answers that cite the revisions their facts came from.

## Outputs
- `wikipulse/qa/retrieve.py`: `retrieve(question) -> Context`, combining (a) entity lookup and graph traversal around the entities mentioned, (b) LLM-generated read-only Cypher for structural questions, (c) vector search over page summaries stored in Neo4j
- `wikipulse/qa/answer.py`: `answer(question) -> Answer` with text, citations (page titles and revision IDs) and the facts used
- `wikipulse/qa/prompts/` for the Cypher and answer prompts
- CLI: `uv run python -m wikipulse.qa.ask "question"`

## Requirements
1. Generated Cypher is read-only (reject anything that writes), has a result limit and a timeout.
2. If the graph has no relevant facts, the answer says so.
3. Every factual claim cites its source.

## Acceptance criteria
- Five questions of different kinds give correct, cited answers: "What happened today?", "Who is involved in event X?", "How are A and B connected?", "Which organizations appear in more than one event this week?", and one unanswerable question.

## Also build
Tests for `wikipulse/qa/` with a mocked LLM and a test Neo4j.

## Design notes
- Start with graph traversal only, then add Cypher generation, then vector search, measuring each with spec 05b.
- Small models write shaky Cypher. Including the graph schema and two examples in the prompt helps a lot.

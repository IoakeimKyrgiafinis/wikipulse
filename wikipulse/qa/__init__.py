"""Question answering over the knowledge graph (GraphRAG, stage 5).

    from wikipulse.qa.answer import answer

    result = answer("How are Ueno Bank and Santiago Peña connected?")
    result.text, result.citations, result.facts

`retrieve.py` collects facts from three channels (graph traversal around the entities
the question names, an LLM-written read-only Cypher query, vector search over page
summaries and events); `answer.py` turns them into a cited answer. `index.py` builds the
search indexes and embeddings they need: `uv run python -m wikipulse.qa.index`.
"""

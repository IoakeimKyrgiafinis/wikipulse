"""Search indexes for question answering: `uv run python -m wikipulse.qa.index`.

Creates (if missing) the full-text indexes over entity names and page titles, then
embeds every page summary and every event (title, description, page titles) that has
no embedding yet or whose text changed since, and creates the vector indexes over
them. Embeddings use the same local model as event grouping (`EVENTS_EMBED_MODEL`,
384 numbers per text) and are stored on the nodes as `embedding`, with a hash of the
embedded text (`embedding_hash`) so a re-run only embeds what changed.

Run it after `wikipulse.graph.run` has written new events. It adds properties and
indexes; it never changes or removes the graph's facts. `--force` re-embeds everything.
"""

import argparse
import hashlib
import logging
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass

from neo4j import Driver

from wikipulse.config import get_settings
from wikipulse.events.embed import Embedder, page_text
from wikipulse.logging_setup import setup_logging
from wikipulse.qa.graph import (
    ENTITY_NAMES_INDEX,
    EVENT_VECTOR_INDEX,
    PAGE_TITLES_INDEX,
    PAGE_VECTOR_INDEX,
    QAGraph,
)

logger = logging.getLogger(__name__)

# bge models embed a short *query* better with this instruction in front; the
# documents (page summaries, events) are embedded without it.
QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "
WRITE_BATCH = 100

FULLTEXT_INDEXES = [
    f"CREATE FULLTEXT INDEX {ENTITY_NAMES_INDEX} IF NOT EXISTS "
    "FOR (n:Entity) ON EACH [n.name, n.aliases] "
    "OPTIONS {indexConfig: {`fulltext.analyzer`: 'standard-folding'}}",
    f"CREATE FULLTEXT INDEX {PAGE_TITLES_INDEX} IF NOT EXISTS "
    "FOR (n:Page) ON EACH [n.title] "
    "OPTIONS {indexConfig: {`fulltext.analyzer`: 'standard-folding'}}",
]


def vector_indexes(dimensions: int) -> list[str]:
    options = (
        "OPTIONS {indexConfig: {"
        f"`vector.dimensions`: {int(dimensions)}, `vector.similarity_function`: 'cosine'}}}}"
    )
    return [
        f"CREATE VECTOR INDEX {PAGE_VECTOR_INDEX} IF NOT EXISTS "
        f"FOR (n:Page) ON n.embedding {options}",
        f"CREATE VECTOR INDEX {EVENT_VECTOR_INDEX} IF NOT EXISTS "
        f"FOR (n:Event) ON n.embedding {options}",
    ]


def event_text(title: str | None, description: str | None, pages: Sequence[str]) -> str:
    """What represents an event in vector search: its headline, description and pages."""
    parts = [part for part in (title, description) if part]
    if pages:
        parts.append("Pages: " + "; ".join(pages))
    return ". ".join(parts)


def text_hash(text: str, model: str) -> str:
    return hashlib.sha256(f"{model}\n{text}".encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Document:
    label: str  # "Page" or "Event"
    id: str  # page title or event key
    text: str
    hash: str


PAGES = """
MATCH (p:Page)
RETURN p.title AS id, p.description AS description, p.summary AS summary,
       p.embedding_hash AS hash
"""
EVENTS = """
MATCH (ev:Event)
OPTIONAL MATCH (p:Page)-[po:PART_OF]->(ev)
WITH ev, p, po ORDER BY po.score DESC
RETURN ev.key AS id, ev.title AS title, ev.description AS description,
       collect(p.title) AS pages, ev.embedding_hash AS hash
"""
SET_PAGE = """
UNWIND $rows AS row
MATCH (p:Page {title: row.id})
SET p.embedding = row.vector, p.embedding_hash = row.hash, p.embedding_model = $model
"""
SET_EVENT = """
UNWIND $rows AS row
MATCH (ev:Event {key: row.id})
SET ev.embedding = row.vector, ev.embedding_hash = row.hash, ev.embedding_model = $model
"""


def documents_to_embed(driver: Driver, model: str, force: bool = False) -> list[Document]:
    """Pages and events whose embedding is missing or was made from another text."""
    docs: list[Document] = []
    with driver.session() as session:
        for row in session.run(PAGES):
            text = page_text(row["id"], row["description"], row["summary"])
            docs.append(Document("Page", row["id"], text, text_hash(text, model)))
            if not force and row["hash"] == docs[-1].hash:
                docs.pop()
        for row in session.run(EVENTS):
            text = event_text(row["title"], row["description"], row["pages"])
            docs.append(Document("Event", row["id"], text, text_hash(text, model)))
            if not force and row["hash"] == docs[-1].hash:
                docs.pop()
    return docs


def ensure_indexes(driver: Driver, dimensions: int) -> None:
    """Create the full-text and vector indexes (does nothing if they exist)."""
    with driver.session() as session:
        for statement in FULLTEXT_INDEXES + vector_indexes(dimensions):
            session.run(statement).consume()
        session.run("CALL db.awaitIndexes(120)").consume()


def write_embeddings(
    driver: Driver, docs: Sequence[Document], vectors: Sequence[Sequence[float]], model: str
) -> None:
    rows = {"Page": [], "Event": []}
    for doc, vector in zip(docs, vectors, strict=True):
        rows[doc.label].append(
            {"id": doc.id, "vector": [float(x) for x in vector], "hash": doc.hash}
        )
    with driver.session() as session:
        for label, statement in (("Page", SET_PAGE), ("Event", SET_EVENT)):
            batch = rows[label]
            for start in range(0, len(batch), WRITE_BATCH):
                chunk = batch[start : start + WRITE_BATCH]
                session.run(statement, rows=chunk, model=model).consume()


def build_index(driver: Driver, embedder: Embedder, force: bool = False) -> dict[str, int]:
    """Embed what changed and make sure every index exists. Returns counts."""
    docs = documents_to_embed(driver, embedder.model_name, force=force)
    vectors = embedder.embed_many([doc.text for doc in docs]) if docs else []
    if docs:
        write_embeddings(driver, docs, vectors, embedder.model_name)
    dimensions = len(vectors[0]) if len(vectors) else len(embedder.embed("dimension probe"))
    ensure_indexes(driver, dimensions)
    return {
        "pages": sum(1 for d in docs if d.label == "Page"),
        "events": sum(1 for d in docs if d.label == "Event"),
        "dimensions": dimensions,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="re-embed every page and event")
    args = parser.parse_args(argv)
    setup_logging()
    settings = get_settings()
    started = time.monotonic()
    embedder = Embedder(settings.events_embed_model)
    with QAGraph.from_settings(settings) as graph:
        counts = build_index(graph.driver, embedder, force=args.force)
    print(
        f"Embedded {counts['pages']} pages and {counts['events']} events "
        f"({counts['dimensions']} dimensions, {embedder.model_name}); "
        f"indexes ready in {time.monotonic() - started:.1f} s."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

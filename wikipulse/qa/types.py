"""What retrieval hands to answering, and what answering returns.

A **fact** is one short statement read from the graph, with the Wikipedia revisions it
came from. The answering model only sees facts (numbered F1, F2, ...) and cites them by
number; `answer.py` turns those numbers into page titles and revision IDs.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Channel = Literal["graph", "cypher", "vector"]
FactKind = Literal["event", "involvement", "relation", "path", "page", "query"]

WIKIPEDIA_URL = "https://en.wikipedia.org/w/index.php"


class Citation(BaseModel):
    """A Wikipedia page at the revision a fact was read from."""

    model_config = {"frozen": True}

    title: str
    revision: int | None = None

    @property
    def url(self) -> str:
        """A permanent link to exactly that revision (or the page, if unknown)."""
        if self.revision is not None:
            return f"{WIKIPEDIA_URL}?oldid={self.revision}"
        return f"{WIKIPEDIA_URL}?title={self.title.replace(' ', '_')}"

    def label(self) -> str:
        revision = f", revision {self.revision}" if self.revision is not None else ""
        return f"{self.title}{revision}"


class Fact(BaseModel):
    """One statement from the graph. `id` is given when it joins a `Context`."""

    id: str = ""
    kind: FactKind
    channel: Channel
    text: str
    sources: list[Citation] = Field(default_factory=list)
    # Identifies the same fact found twice (e.g. by traversal and by a query).
    key: str = ""


class EntityRef(BaseModel):
    """An entity of the graph that the question names."""

    key: str
    name: str
    type: str | None = None
    wikidata_id: str | None = None
    matched: str = ""  # the words of the question that named it


class Context(BaseModel):
    """Everything retrieval found for one question."""

    question: str
    as_of: datetime | None = None
    facts: list[Fact] = Field(default_factory=list)
    entities: list[EntityRef] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)  # keys of the events looked at
    cypher: str | None = None
    cypher_rows: int | None = None
    notes: list[str] = Field(default_factory=list)  # what went wrong or found nothing
    seconds: dict[str, float] = Field(default_factory=dict)
    max_facts: int = 60

    def add(self, fact: Fact) -> Fact | None:
        """Add `fact` with the next id, unless the same fact is there or the context is
        full. Returns the fact as stored (or the earlier copy), None if it was dropped."""
        key = fact.key or fact.text
        for known in self.facts:
            if known.key == key:
                return known
        if len(self.facts) >= self.max_facts:
            return None
        stored = fact.model_copy(update={"id": f"F{len(self.facts) + 1}", "key": key})
        self.facts.append(stored)
        return stored

    def fact(self, fact_id: str) -> Fact | None:
        return next((f for f in self.facts if f.id == fact_id), None)

    def channel_counts(self) -> dict[str, int]:
        counts = {"graph": 0, "cypher": 0, "vector": 0}
        for fact in self.facts:
            counts[fact.channel] += 1
        return counts


class Answer(BaseModel):
    """A grounded answer: text with [n] markers, the citations they point to, and the
    facts the answer used."""

    question: str
    text: str  # with citation numbers: "... resigned [1][2]."
    answerable: bool
    fact_text: str = ""  # the same answer with the fact ids the model cited: "[F3]"
    citations: list[Citation] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    model: str | None = None
    uncited_sentences: list[str] = Field(default_factory=list)
    context: Context | None = None
    seconds: dict[str, float] = Field(default_factory=dict)

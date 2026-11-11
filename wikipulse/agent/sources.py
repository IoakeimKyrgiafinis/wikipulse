"""The sources an agent run has seen, numbered so the model can cite them.

Every Wikipedia page revision (or statement backed by several) that a tool result
shows gets a short id, [F1], [F2], ..., written into the result next to what it
backs. The model cites those ids in its briefing; the code then checks them and turns
them into page titles with revision links. The model never writes a URL itself.

Under the hood this is the `Context` of question answering (`wikipulse.qa.types`):
each id is a `Fact`, so the citation checks of `wikipulse.qa.answer` (unknown ids,
uncited sentences, numbering) work on briefings unchanged.

The book also keeps, per id, the text the run actually *read* from that source
(`note()`, `evidence`): a page's text from `read_page`, a relation line, an answer of
`ask`. A line that only names a source (the event's list of pages, the list of who is
involved) is not evidence. The grounding check shows each sentence exactly this
evidence, and a citation of a source the run never read is dropped.
"""

from collections.abc import Iterable
from typing import Literal

from wikipulse.qa.types import Citation, Context, Fact

MAX_SOURCES = 300  # far more than one run shows; protects against a runaway loop


class ExtractionCitation(Citation):
    """Not a Wikipedia text: an event's title and description, written by WikiPulse's
    extraction model (stage 4) from the event's pages. Cited as what it is, so a
    briefing never presents the model's summary as something a Wikipedia revision says."""

    kind: Literal["extraction"] = "extraction"

    @property
    def url(self) -> str:
        return ""  # there is no page to link to

    def label(self) -> str:
        return f"{self.title} (written by WikiPulse's extraction model, not a Wikipedia text)"


def extraction_citation(event_key: str) -> ExtractionCitation:
    """The citation of an event's extracted title and description."""
    return ExtractionCitation(title=f"WikiPulse event {event_key}: title and description")


class SourceBook:
    """Hands out source ids and remembers what each one stands for and what was read
    from it."""

    def __init__(self, label: str = "") -> None:
        self.context = Context(question=label, max_facts=MAX_SOURCES)
        # The graph key of the event under investigation, once known: page reads cite
        # the revision of *this* event (see `QAGraph.page_text`).
        self.event_key: str | None = None
        self.evidence: dict[str, list[str]] = {}  # id -> texts read from that source

    def cite(self, citation: Citation) -> str:
        """The id of one page revision (the same id every time it shows up)."""
        kind = getattr(citation, "kind", "page")
        fact = Fact(
            kind="event" if kind == "extraction" else "page",
            channel="graph",
            text=citation.label() if kind == "extraction" else f"Wikipedia page {citation.label()}",
            sources=[citation],
            key=f"{kind}:{citation.title}:{citation.revision}",
        )
        return self._add(fact)

    def statement(self, text: str, sources: Iterable[Citation]) -> str:
        """The id of a statement backed by several pages together.
        With a single source this is just that page's id."""
        unique = list(dict.fromkeys(sources))
        if len(unique) == 1:
            return self.cite(unique[0])
        if not unique:
            return ""
        fact = Fact(kind="event", channel="graph", text=text, sources=unique, key=text)
        return self._add(fact)

    def note(self, source_id: str, text: str) -> None:
        """Remember `text` as something the run read from source `source_id`."""
        if source_id and text and text not in self.evidence.setdefault(source_id, []):
            self.evidence[source_id].append(text)

    def _add(self, fact: Fact) -> str:
        stored = self.context.add(fact)
        return stored.id if stored is not None else ""

    @property
    def ids(self) -> set[str]:
        return {fact.id for fact in self.context.facts}

    def __len__(self) -> int:
        return len(self.context.facts)


def marker(*ids: str) -> str:
    """ "[F1][F3]" for the given ids (empty ids are skipped)."""
    return "".join(f"[{source_id}]" for source_id in dict.fromkeys(ids) if source_id)

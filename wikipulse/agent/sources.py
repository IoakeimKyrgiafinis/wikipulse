"""The sources an agent run has seen, numbered so the model can cite them.

Every Wikipedia page revision (or statement backed by several) that a tool result
shows gets a short id, [F1], [F2], ..., written into the result next to what it
backs. The model cites those ids in its briefing; the code then checks them and turns
them into page titles with revision links. The model never writes a URL itself.

Under the hood this is the `Context` of question answering (`wikipulse.qa.types`):
each id is a `Fact`, so the citation checks of `wikipulse.qa.answer` (unknown ids,
uncited sentences, numbering) work on briefings unchanged.
"""

from collections.abc import Iterable

from wikipulse.qa.types import Citation, Context, Fact

MAX_SOURCES = 300  # far more than one run shows; protects against a runaway loop


class SourceBook:
    """Hands out source ids and remembers what each one stands for."""

    def __init__(self, label: str = "") -> None:
        self.context = Context(question=label, max_facts=MAX_SOURCES)

    def cite(self, citation: Citation) -> str:
        """The id of one page revision (the same id every time it shows up)."""
        fact = Fact(
            kind="page",
            channel="graph",
            text=f"Wikipedia page {citation.label()}",
            sources=[citation],
            key=f"page:{citation.title}:{citation.revision}",
        )
        return self._add(fact)

    def statement(self, text: str, sources: Iterable[Citation]) -> str:
        """The id of a statement backed by several pages (e.g. an event description).
        With a single source this is just that page's id."""
        unique = list(dict.fromkeys(sources))
        if len(unique) == 1:
            return self.cite(unique[0])
        if not unique:
            return ""
        fact = Fact(kind="event", channel="graph", text=text, sources=unique, key=text)
        return self._add(fact)

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

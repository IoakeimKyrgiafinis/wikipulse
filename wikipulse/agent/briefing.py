"""From the model's draft to a checked, cited briefing.

The model's last answer is a draft. Code, not the model, decides what is published:

1. Links go (`strip_links()`): a briefing cites with source ids, and the code writes
   the permanent Wikipedia links itself. A link in the draft can only come from the data
   (or from an attacker's text in it), so it is removed whatever it points to.
2. Source ids the run never handed out are removed (`drop_unknown_ids()` of 05a), and
   so are ids of sources the run never read anything from (`drop_unread_citations()`:
   a page it only saw in a list backs nothing).
3. Sentences without a source id are found (`uncited_sentences()` of 05a); the agent
   loop asks the model once to fix them, and those still left are removed.
4. A grounding check (optional, AGENT_VERIFY): the model gets each sentence with the
   text the run read from the sources it cites (`evidence()`), and says per sentence
   whether that text states it. It fails closed: a sentence without an explicit
   "supported" verdict is removed.
5. Ids become numbered citations, [F3] -> [1], each a page title at a revision with its
   permanent link (`resolve_citations()` of 05a), or the event's description, cited as
   written by the extraction model (`ExtractionCitation`).

A draft with no cited sentence left is no briefing at all.
"""

import re

from pydantic import BaseModel, Field, SerializeAsAny

from wikipulse.agent.render import DOMAIN, URL
from wikipulse.agent.sources import SourceBook
from wikipulse.qa.answer import (
    cited_ids,
    claims,
    drop_sentences,
    drop_unknown_ids,
    resolve_citations,
    uncited_sentences,
)
from wikipulse.qa.types import Citation

_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\(\s*[^)\s]*\s*\)")
# What a removed link leaves behind: "()", "(see )", "<>".
_EMPTY_BRACKETS = re.compile(r"\s*\(\s*(?:see|at|visit|via|from)?\s*\)|<\s*>", re.IGNORECASE)
_SPACES = re.compile(r"[ \t]{2,}")
# A source list the model wrote itself, from a "Sources:" line to the end.
UNVERIFIED = "Note: this briefing could not be checked against its sources."
_SOURCE_LIST = re.compile(r"^\W*Sources?\W*:.*\Z", re.IGNORECASE | re.MULTILINE | re.DOTALL)


class Briefing(BaseModel):
    """A checked briefing: text with [n] markers and the citations they point to."""

    text: str
    # SerializeAsAny: an `ExtractionCitation` keeps its `kind` in the transcript.
    citations: list[SerializeAsAny[Citation]] = Field(default_factory=list)
    removed_sentences: list[str] = Field(default_factory=list)
    removed_links: list[str] = Field(default_factory=list)
    # True: passed the grounding check; False: the check could not run; None: not asked.
    verified: bool | None = None

    def as_text(self) -> str:
        """What is stored on the event: the briefing, then its numbered sources."""
        sources = [
            f"[{n}] {c.label()}" + (f": {c.url}" if c.url else "")
            for n, c in enumerate(self.citations, start=1)
        ]
        note = f"\n\n{UNVERIFIED}" if self.verified is False else ""
        return self.text + note + "\n\nSources:\n" + "\n".join(sources)


def strip_links(text: str) -> tuple[str, list[str]]:
    """`text` without links (markdown, URLs, bare domains); returns the links removed."""
    removed: list[str] = []

    def drop(match: re.Match[str]) -> str:
        removed.append(match.group(0))
        return ""

    def keep_label(match: re.Match[str]) -> str:
        removed.append(match.group(0))
        return match.group(1)

    text = _MARKDOWN_LINK.sub(keep_label, text)
    text = URL.sub(drop, text)
    text = DOMAIN.sub(drop, text)
    text = _EMPTY_BRACKETS.sub("", text)
    lines = [_SPACES.sub(" ", line).rstrip() for line in text.splitlines()]
    text = "\n".join(lines)
    return re.sub(r"\s+([.,;:!?])", r"\1", text).strip(), removed


def clean_draft(draft: str, book: SourceBook) -> tuple[str, list[str]]:
    """Steps 1 and 2: no links, no unknown ids, no source list of the model's own."""
    text = _SOURCE_LIST.sub("", draft).strip()  # the code writes the source list
    text, links = strip_links(text)
    return drop_unknown_ids(text, book.ids), links


def missing_citations(text: str) -> list[str]:
    """The briefing's sentences that cite no source."""
    return uncited_sentences(text)


def finish(
    text: str, book: SourceBook, links: list[str], unsupported: list[str] | None = None
) -> Briefing | None:
    """Steps 3 to 5 on an already cleaned text: drop what is still uncited (and the
    sentences the check found unsupported), number the citations. None when nothing
    cited is left."""
    removed = uncited_sentences(text) + list(unsupported or [])
    text = drop_sentences(text, removed) if removed else text
    if not cited_ids(text) or not claims(text):
        return None
    final, citations, _ = resolve_citations(text, book.context)
    if not citations:
        return None
    return Briefing(text=final, citations=citations, removed_sentences=removed, removed_links=links)


# --- Step 5: the grounding check --------------------------------------------------------
VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"n": {"type": "integer"}, "supported": {"type": "boolean"}},
                "required": ["n", "supported"],
            },
        }
    },
    "required": ["verdicts"],
}


def evidence(source_ids: list[str], book: SourceBook, tool_texts: list[str]) -> list[str]:
    """What the run read from these sources (`SourceBook.note`), as far as the model was
    shown it: a page's text, a relation line, an answer. A line that only names a source
    (the event's list of pages) is no evidence."""
    shown = "\n\n".join(tool_texts)
    found = [
        text
        for source_id in source_ids
        for text in book.evidence.get(source_id, [])
        if text in shown  # not cut off before the model saw it
    ]
    return list(dict.fromkeys(found))


def drop_unread_citations(text: str, book: SourceBook, tool_texts: list[str]) -> str:
    """`text` without citations of sources the run never read anything from (a page only
    seen in a list); a sentence left without any citation is then dropped as uncited."""
    read = {source_id for source_id in book.ids if evidence([source_id], book, tool_texts)}
    return re.sub(r"[ \t]+([.,;:!?])", r"\1", drop_unknown_ids(text, read))


def verification_request(
    text: str, book: SourceBook, tool_texts: list[str]
) -> tuple[str, list[str]]:
    """The message for the check, and the sentences in the order they are numbered."""
    sentences = [s for s in claims(text) if cited_ids(s)]
    parts = []
    for n, sentence in enumerate(sentences, start=1):
        lines = evidence(cited_ids(sentence), book, tool_texts) or ["(none)"]
        header = f"Sentence {n}: {sentence}\nEvidence for sentence {n}:\n"
        parts.append(header + "\n".join(lines))
    return "\n\n".join(parts), sentences


def unsupported_sentences(data: object, sentences: list[str]) -> list[str]:
    """The sentences the check did not explicitly mark as supported. Fails closed: a
    missing, malformed or contradictory verdict counts as "not supported"."""
    verdicts = data.get("verdicts", []) if isinstance(data, dict) else []
    verdicts = verdicts if isinstance(verdicts, list) else []
    said: dict[object, set[bool]] = {}
    for verdict in verdicts:
        if isinstance(verdict, dict):
            said.setdefault(verdict.get("n"), set()).add(verdict.get("supported") is True)
    return [s for n, s in enumerate(sentences, start=1) if said.get(n) != {True}]

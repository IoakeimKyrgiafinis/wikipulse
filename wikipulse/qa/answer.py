"""Grounded answers: `answer(question) -> Answer`.

The answering model sees the question and the retrieved facts, numbered F1, F2, ...
(`prompts/answer_v1.md`), and must end every sentence with the ids of the facts it
comes from. Then the code, not the model, checks and turns the ids into citations:

- ids that don't exist are removed;
- sentences without any id are found (`uncited_sentences()`); the model gets one chance
  to fix them, and those still left are reported in `Answer.uncited_sentences`;
- an "answerable" answer that cites no fact at all is replaced by a refusal, because
  nothing in it could be checked;
- every fact id becomes the numbers of its sources: [F3] -> [1][2], where [1] is a page
  title and the revision the fact was read from (`Answer.citations`).

With no facts at all, the answer is a refusal without asking the model.
"""

import logging
import re
import time
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from wikipulse.config import Settings, get_settings
from wikipulse.events.embed import Embedder
from wikipulse.llm import LLMClient, Message, get_client
from wikipulse.qa.cypher import CypherWriter
from wikipulse.qa.graph import QAGraph
from wikipulse.qa.retrieve import ALL_CHANNELS, RetrievalConfig, Retriever
from wikipulse.qa.types import Answer, Channel, Citation, Context, Fact

logger = logging.getLogger(__name__)

PROMPT_VERSION = "answer_v1"
PROMPTS_DIR = Path(__file__).parent / "prompts"
NO_FACTS = "The knowledge graph has no facts about this question."
NOTHING_CITED = (
    "The knowledge graph has facts near this question, but none that answer it with a "
    "source I can cite."
)
RETRY_PROMPT = (
    "These sentences of your answer have no fact ids:\n{sentences}\n\n"
    "Answer again, with the same JSON format. End every sentence with the ids of the "
    "facts that support it, and leave out any sentence that no fact supports."
)

_MARKER = re.compile(r"\[\s*(F\d+(?:\s*[,;]\s*F\d+)*)\s*\]")
_NUMBER = re.compile(r"\[\d+\]")
_MARKER_RUN = re.compile(rf"{_MARKER.pattern}(?:\s*{_MARKER.pattern})*")
_SPACED_RUN = re.compile(rf"(\s*)({_MARKER_RUN.pattern})")
# A sentence ends at . ! or ? before a space, but not after an initial or abbreviation
# such as "U.S." or "J." (a capital letter and a dot).
_SENTENCE_END = re.compile(r"(?<![A-Z]\.)(?<=[.!?])\s+(?=\S)")

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"answerable": {"type": "boolean"}, "answer": {"type": "string"}},
    "required": ["answerable", "answer"],
    "additionalProperties": False,
}


def load_prompt(version: str = PROMPT_VERSION) -> str:
    return (PROMPTS_DIR / f"{version}.md").read_text(encoding="utf-8")


def build_message(context: Context) -> str:
    lines = [f"Question: {context.question}", ""]
    if context.as_of is not None:
        lines += [f"Current time (newest event in the graph): {context.as_of.isoformat()}", ""]
    lines.append("Facts:")
    lines += [f"[{fact.id}] {fact.text}" for fact in context.facts]
    if context.notes:
        lines += ["", "Search notes (not facts, do not cite):"]
        lines += [f"- {note}" for note in context.notes]
    return "\n".join(lines)


def cited_ids(text: str) -> list[str]:
    """Fact ids in the order they are first cited: "[F2][F5, F1]" -> F2, F5, F1."""
    ids: list[str] = []
    for group in _MARKER.findall(text):
        ids += [part.strip() for part in re.split(r"[,;]", group)]
    return list(dict.fromkeys(ids))


def drop_unknown_ids(text: str, known: set[str]) -> str:
    """Remove citations of fact ids that don't exist."""

    def keep(match: re.Match[str]) -> str:
        ids = [part.strip() for part in re.split(r"[,;]", match.group(1))]
        valid = [fact_id for fact_id in ids if fact_id in known]
        return "".join(f"[{fact_id}]" for fact_id in valid)

    return _MARKER.sub(keep, text)


def strip_markers(text: str) -> str:
    """The text without fact ids (for refusals, which cite nothing)."""
    return re.sub(rf"\s*{_MARKER_RUN.pattern}", "", text).strip()


def claims(text: str) -> list[str]:
    """Sentences and list items that make a claim (not headings like "Events:")."""
    pieces = []
    for line in text.splitlines():
        for piece in _SENTENCE_END.split(line.strip()):
            piece = piece.strip().lstrip("-*• ").strip()
            words = _MARKER.sub("", piece).split()
            if len(words) >= 3 and not piece.endswith(":"):
                pieces.append(piece)
    return pieces


def uncited_sentences(text: str) -> list[str]:
    return [piece for piece in claims(text) if not _MARKER.search(piece)]


def resolve_citations(text: str, context: Context) -> tuple[str, list[Citation], list[Fact]]:
    """Replace fact ids by citation numbers; return the text, citations and facts used."""
    citations: list[Citation] = []
    facts: list[Fact] = []
    for fact_id in cited_ids(text):
        fact = context.fact(fact_id)
        if fact is None:
            continue
        facts.append(fact)
        for source in fact.sources:
            if source not in citations:
                citations.append(source)

    def numbers(match: re.Match[str]) -> str:
        # A run of markers ("[F2][F5, F1]") becomes one sorted set of numbers; a run
        # whose facts have no source disappears with the space before it.
        result: set[int] = set()
        for fact_id in cited_ids(match.group(2)):
            fact = context.fact(fact_id)
            result.update(citations.index(source) + 1 for source in (fact.sources if fact else []))
        if not result:
            return ""
        return match.group(1) + "".join(f"[{number}]" for number in sorted(result))

    return _SPACED_RUN.sub(numbers, text), citations, facts


class Answerer:
    """Writes a cited answer from a retrieved context."""

    def __init__(self, client: LLMClient, prompt_version: str = PROMPT_VERSION) -> None:
        self.client = client
        self.prompt_version = prompt_version
        self.prompt = load_prompt(prompt_version)

    def _ask(self, messages: list[Message]) -> tuple[bool, str]:
        response = self.client.chat(
            messages, system=self.prompt, json_schema=ANSWER_SCHEMA, temperature=0
        )
        data = response.data if isinstance(response.data, dict) else {}
        return bool(data.get("answerable")), str(data.get("answer", "")).strip()

    def answer(self, context: Context) -> Answer:
        started = time.monotonic()
        if not context.facts:
            return Answer(
                question=context.question,
                text=NO_FACTS,
                answerable=False,
                model=None,
                context=context,
            )
        known = {fact.id for fact in context.facts}
        messages = [Message.user(build_message(context))]
        answerable, text = self._ask(messages)
        text = drop_unknown_ids(text, known)
        missing = uncited_sentences(text) if answerable else []
        if missing:
            logger.info("Answer has %d uncited sentences; asking again", len(missing))
            reply = RETRY_PROMPT.format(sentences="\n".join(f"- {s}" for s in missing))
            messages += [Message.assistant(text), Message.user(reply)]
            answerable, text = self._ask(messages)
            text = drop_unknown_ids(text, known)
            missing = uncited_sentences(text) if answerable else []
        fact_text = text
        if answerable and not cited_ids(text):
            answerable, text, missing = False, NOTHING_CITED, []
        if not answerable:
            final, citations, facts = strip_markers(text), [], []
        else:
            final, citations, facts = resolve_citations(text, context)
            # A sentence that cites only facts without a source has no page to show.
            missing += [c for c in claims(final) if not _NUMBER.search(c) and c not in missing]
        return Answer(
            question=context.question,
            text=final,
            fact_text=fact_text,
            answerable=answerable,
            citations=citations,
            facts=facts,
            model=self.client.setting,
            uncited_sentences=missing,
            context=context,
            seconds={"answer": round(time.monotonic() - started, 3)},
        )


class QASession:
    """Everything question answering needs, created once: the graph connection, the LLM
    client, the embedding model, the retriever and the answerer."""

    def __init__(
        self, graph: QAGraph, client: LLMClient, retriever: Retriever, answerer: Answerer
    ) -> None:
        self.graph = graph
        self.client = client
        self.retriever = retriever
        self.answerer = answerer

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        channels: Iterable[Channel] = ALL_CHANNELS,
        model: str | None = None,
    ) -> "QASession":
        graph = QAGraph.from_settings(settings)
        client = get_client(model or settings.qa_model, settings)
        retriever = Retriever(
            graph,
            cypher_writer=CypherWriter(client),
            embedder=Embedder(settings.events_embed_model),
            config=RetrievalConfig.from_settings(settings),
            channels=channels,
        )
        return cls(graph, client, retriever, Answerer(client))

    def ask(self, question: str, as_of: datetime | None = None) -> Answer:
        started = time.monotonic()
        context = self.retriever.retrieve(question, as_of=as_of)
        retrieved = time.monotonic()
        result = self.answerer.answer(context)
        result.seconds["retrieve"] = round(retrieved - started, 3)
        result.seconds["total"] = round(time.monotonic() - started, 3)
        return result

    def close(self) -> None:
        self.client.close()
        self.graph.close()

    def __enter__(self) -> "QASession":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def answer(
    question: str,
    as_of: datetime | None = None,
    channels: Iterable[Channel] = ALL_CHANNELS,
    settings: Settings | None = None,
) -> Answer:
    """Answer one question from the knowledge graph, with citations."""
    with QASession.from_settings(settings or get_settings(), channels=channels) as session:
        return session.ask(question, as_of=as_of)

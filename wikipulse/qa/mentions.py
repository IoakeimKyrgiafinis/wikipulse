"""Which entities does a question name?

Two steps, so the graph can be large: the full-text index (`qa_entity_names`) proposes
candidates that share any word with the question, then `find_mentions()` keeps only
those whose whole name (or alias) appears in the question as a run of words. Where
names overlap ("Democratic Party" and "Party"), the longest wins.

Comparison is on a folded form (`fold()`): lower case, accents removed, punctuation as
spaces, so "Santiago Pena", "santiago peña" and "Santiago Peña" are the same words.
"""

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from wikipulse.qa.graph import EntityCandidate

# Words that never start a search on their own (they are in most questions).
STOPWORDS = frozenset(
    """a an and are as at be been by did do does for from had has have how i in into is it
    its me of on or over than that the their them then there these they this those to
    was were what when where which who whom whose why will with about after before
    between more most other some such today yesterday week weeks month day days happen
    happened happening involved connected connection related appear appears event events
    news tell know any all many much one two get got latest recent recently now new""".split()
)
MIN_NAME_CHARS = 3
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)


def fold(text: str) -> str:
    """Lower case, no accents, words separated by single spaces."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(_NON_WORD.sub(" ", plain.casefold()).split())


@dataclass(frozen=True)
class Mention:
    candidate: EntityCandidate
    matched: str  # the folded words of the question
    start: int  # word positions in the folded question
    end: int


def _spans(words: list[str], name_words: list[str]) -> Iterable[tuple[int, int]]:
    size = len(name_words)
    for start in range(len(words) - size + 1):
        if words[start : start + size] == name_words:
            yield start, start + size


def _acceptable(name: str, question: str) -> bool:
    """Short names ("US", "UN") count only if written exactly so in the question, so the
    word "us" doesn't name the United States; names of stopwords never count."""
    folded = fold(name)
    if not folded or all(word in STOPWORDS for word in folded.split()):
        return False
    if len(folded.replace(" ", "")) < MIN_NAME_CHARS:
        return re.search(rf"(?<!\w){re.escape(name)}(?!\w)", question) is not None
    return True


def find_mentions(question: str, candidates: Iterable[EntityCandidate]) -> list[Mention]:
    """The candidates whose name or alias the question contains, longest first, without
    overlaps; each entity at most once."""
    words = fold(question).split()
    found: list[Mention] = []
    for candidate in candidates:
        for name in dict.fromkeys((candidate.name, *candidate.aliases)):
            if not name or not _acceptable(name, question):
                continue
            name_words = fold(name).split()
            for start, end in _spans(words, name_words):
                found.append(Mention(candidate, " ".join(name_words), start, end))
    found.sort(key=lambda m: (-(m.end - m.start), m.start, m.candidate.key))
    taken: set[int] = set()
    keys: set[str] = set()
    chosen: list[Mention] = []
    for mention in found:
        positions = set(range(mention.start, mention.end))
        if mention.candidate.key in keys:
            continue
        if positions & taken:
            # The same words may name two entities (e.g. a person and a page title
            # spelled alike); keep both only if the span is exactly the same.
            if not any(m.start == mention.start and m.end == mention.end for m in chosen):
                continue
        taken |= positions
        keys.add(mention.candidate.key)
        chosen.append(mention)
    chosen.sort(key=lambda m: m.start)
    return chosen


def find_titles(question: str, titles: Iterable[str]) -> list[str]:
    """Page titles that appear in the question as a run of words, longest first, without
    overlaps ("2026 Paraguay banking crisis", not also "Banking")."""
    words = fold(question).split()
    spans = []
    for title in titles:
        if _acceptable(title, question):
            spans += [(start, end, title) for start, end in _spans(words, fold(title).split())]
    spans.sort(key=lambda span: (span[0] - span[1], span[0]))
    taken: set[int] = set()
    found: list[str] = []
    for start, end, title in spans:
        positions = set(range(start, end))
        if not positions & taken and title not in found:
            taken |= positions
            found.append(title)
    return found


_EVENT_NUMBER = re.compile(r"\bevent\s*(?:#|no\.?|number)?\s*(\d+)\b", re.IGNORECASE)
_EVENT_KEY = re.compile(r"\b([\w.-]+:\d+)\b")


def event_references(question: str) -> tuple[list[int], list[str]]:
    """Events named by number ("event 341") or key ("03b-default-fix:341")."""
    keys = _EVENT_KEY.findall(question)
    numbers = [int(n) for n in _EVENT_NUMBER.findall(question)]
    return numbers, keys

"""Comparing names: the same entity is often written slightly differently."""

import re
import unicodedata

_PARENTHETICAL = re.compile(r"\s*\([^()]*\)$")
_QUOTES = "\"'“”‘’«»"


def clean_name(name: str) -> str:
    """A name as the model wrote it, without surrounding quotes and extra spaces."""
    return " ".join(name.strip().strip(_QUOTES).split())


def normalize_name(name: str) -> str:
    """A key for comparing names: Unicode-normalized, case-folded, `_` as space, no
    leading "the". "The_Beatles" and "the beatles" get the same key."""
    text = unicodedata.normalize("NFKC", name).replace("_", " ")
    text = " ".join(clean_name(text).casefold().split())
    return text.removeprefix("the ")


_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_DATE = re.compile(
    rf"^(\d{{1,2}} )?({_MONTHS})( \d{{1,2}})?,? ?(\d{{4}})?$|^\d{{4}}(s|–\d{{2,4}})?$",
    re.IGNORECASE,
)


def is_proper_name(name: str) -> bool:
    """True for something that can be an entity: it starts with a capital letter or a
    digit, after an optional "the" (English names do: "Ueno Bank", "2026 Paraguay
    banking crisis", "the Senate"), and is not a bare date or year. "a teacher",
    "by June 2026", "October 15, 2026" and "20 years" are not entities, however often a small model
    lists them. (The price: lower-case brand names like "iPhone" are dropped too.)"""
    text = clean_name(name)
    if text.casefold().startswith("the "):
        text = text[4:]
    first = next((char for char in text if char.isalnum()), "")
    starts_like_a_name = first.isupper() or first.isdigit()
    has_capital = any(char.isupper() for char in text)  # "149" and "20 years" don't
    return starts_like_a_name and has_capital and not _DATE.match(text)


def without_qualifier(title: str) -> str:
    """A Wikipedia title without its disambiguating qualifier: "Paris (band)" -> "Paris"."""
    return _PARENTHETICAL.sub("", title)

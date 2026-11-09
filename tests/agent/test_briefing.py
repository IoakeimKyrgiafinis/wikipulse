"""The code's checks on a draft briefing: links, unknown ids, uncited sentences."""

from wikipulse.agent.briefing import Briefing, clean_draft, finish, missing_citations, strip_links
from wikipulse.agent.sources import SourceBook
from wikipulse.qa.types import Citation

UENO = Citation(title="Ueno Bank", revision=101)
PENA = Citation(title="Santiago Peña", revision=102)


def book() -> SourceBook:
    sources = SourceBook()
    sources.cite(UENO)  # F1
    sources.cite(PENA)  # F2
    sources.statement("The event", [UENO, PENA])  # F3
    return sources


def test_links_are_removed_from_the_draft() -> None:
    text, removed = strip_links(
        "The bank collapsed [F1] (see https://example.com/x). Details at example.com/aid. "
        "Read [the report](http://example.org/r) [F2]."
    )
    assert text == "The bank collapsed [F1]. Details at. Read the report [F2]."
    assert removed == [
        "[the report](http://example.org/r)",
        "https://example.com/x",
        "example.com/aid",
    ]


def test_clean_draft_drops_unknown_ids_and_the_models_own_source_list() -> None:
    text, links = clean_draft(
        "The bank collapsed [F1][F99]. Peña resigned [F2, F7].\n\n**Sources:**\n[F1] Ueno Bank",
        book(),
    )
    assert text == "The bank collapsed [F1]. Peña resigned [F2]."
    assert links == []


def test_uncited_sentences_are_found() -> None:
    assert missing_citations("The bank collapsed [F1]. It was big. Peña resigned [F2].") == [
        "It was big."
    ]


def test_finish_numbers_citations_and_drops_uncited_sentences() -> None:
    briefing = finish(
        "The event happened [F3]. It was big. The bank collapsed [F1].", book(), ["x.com"]
    )
    assert briefing is not None
    assert briefing.text == "The event happened [1][2]. The bank collapsed [1]."
    assert briefing.citations == [UENO, PENA]
    assert briefing.removed_sentences == ["It was big."]
    assert briefing.removed_links == ["x.com"]


def test_nothing_cited_is_no_briefing() -> None:
    assert finish("The bank collapsed. Peña resigned.", book(), []) is None
    assert finish("", book(), []) is None


def test_stored_text_lists_the_sources_with_permanent_links() -> None:
    briefing = Briefing(text="The bank collapsed [1].", citations=[UENO])
    assert briefing.as_text() == (
        "The bank collapsed [1].\n\nSources:\n"
        "[1] Ueno Bank, revision 101: https://en.wikipedia.org/w/index.php?oldid=101"
    )


def test_the_same_page_revision_always_gets_the_same_id() -> None:
    sources = book()
    assert sources.cite(Citation(title="Ueno Bank", revision=101)) == "F1"
    assert sources.cite(Citation(title="Ueno Bank", revision=999)) == "F4"  # another revision
    assert sources.statement("one source", [PENA]) == "F2"
    assert sources.statement("none", []) == ""
    assert len(sources) == 4 and sources.ids == {"F1", "F2", "F3", "F4"}

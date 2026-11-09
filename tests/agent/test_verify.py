"""The grounding check: each sentence against the text the run read from its sources."""

import json

from tests.agent.helpers import DOWN, call, response, run_agent
from wikipulse.agent.briefing import (
    UNVERIFIED,
    drop_unread_citations,
    evidence,
    unsupported_sentences,
    verification_request,
)
from wikipulse.agent.loop import VERIFY_MAX_TOKENS, VERIFY_PROMPT_VERSION, load_prompt
from wikipulse.agent.render import render_event
from wikipulse.agent.sources import SourceBook

EVENT = {
    "event": {"event_id": "run:7", "title": "Ueno Bank collapses", "description": "It fell."},
    "pages": [
        {"title": "Ueno Bank", "revision": 101},
        {"title": "Santiago Peña", "revision": 102},
    ],
    "entities": [
        {
            "name": "Santiago Peña",
            "type": "person",
            "source": {"title": "Santiago Peña", "revision": 102},
        },
    ],
    "relations": [
        {
            "subject": "Santiago Peña",
            "relation": "resigned as president of",
            "object": "Paraguay",
            "source": {"title": "Ueno Bank", "revision": 101},
        }
    ],
}
PAGE = (
    "Wikipedia page 'Ueno Bank' [F1] (revision 101)\n<untrusted source=\"Ueno Bank\">\n"
    "Ueno Bank was a Paraguayan bank.\n</untrusted>"
)


def investigated(read_page: bool = True) -> tuple[SourceBook, list[str]]:
    """A run's sources after get_event (F1 Ueno Bank, F2 Peña, F3 the extraction) and,
    if asked, a read of Ueno Bank's page."""
    book = SourceBook()
    texts = [render_event(EVENT, book)]
    if read_page:
        book.note("F1", PAGE)
        texts.append(PAGE)
    return book, texts


def verdicts(*supported: bool) -> str:
    return json.dumps(
        {"verdicts": [{"n": n, "supported": s} for n, s in enumerate(supported, start=1)]}
    )


def test_evidence_is_what_the_run_read_from_the_source() -> None:
    book, texts = investigated()
    found = evidence(["F1"], book, texts)
    assert found == ["- Santiago Peña resigned as president of Paraguay. [F1]", PAGE]
    # The extraction's text is evidence for the extraction, not for the pages.
    assert evidence(["F3"], book, texts) == [
        "Event run:7: Ueno Bank collapses [F3]",
        "Description (written by WikiPulse's extraction model from the pages): It fell. [F3]",
    ]
    # Peña's page only shows up in the list of pages and of who is involved: no evidence.
    assert evidence(["F2"], book, texts) == []
    # What the model was never shown (a result cut short) is no evidence either.
    assert evidence(["F1"], book, texts[:1]) == [found[0]]


def test_citations_of_sources_never_read_are_dropped() -> None:
    book, texts = investigated(read_page=False)
    text = "Peña resigned [F1][F2]. Peña is a politician [F2]. It fell [F3]."
    assert drop_unread_citations(text, book, texts) == (
        "Peña resigned [F1]. Peña is a politician. It fell [F3]."
    )


def test_the_request_numbers_cited_sentences() -> None:
    book, texts = investigated()
    text = "Ueno Bank collapsed [F3]. Peña resigned [F1]."
    request, sentences = verification_request(text, book, texts)
    assert sentences == ["Ueno Bank collapsed [F3].", "Peña resigned [F1]."]
    assert request.startswith("Sentence 1: Ueno Bank collapsed [F3].\nEvidence for sentence 1:\n")
    assert "Sentence 2: Peña resigned [F1].\nEvidence for sentence 2:\n" in request
    assert "Ueno Bank was a Paraguayan bank." in request.split("Sentence 2")[1]


def test_the_check_fails_closed() -> None:
    sentences = ["A [F1].", "B [F2].", "C [F3]."]
    assert unsupported_sentences(json.loads(verdicts(True, False, True)), sentences) == ["B [F2]."]
    # Missing, empty, malformed or contradictory verdicts all count as "not supported".
    assert unsupported_sentences(json.loads(verdicts(True)), sentences) == sentences[1:]
    assert unsupported_sentences({"verdicts": []}, sentences) == sentences
    assert unsupported_sentences(None, sentences) == sentences
    assert unsupported_sentences({"verdicts": "yes"}, sentences) == sentences
    wrong_n = {"verdicts": [{"n": 9, "supported": True}, {"n": 1, "supported": "yes"}]}
    assert unsupported_sentences(wrong_n, sentences) == sentences
    both = {"verdicts": [{"n": 1, "supported": True}, {"n": 1, "supported": False}]}
    assert unsupported_sentences(both, sentences[:1]) == sentences[:1]


async def test_unsupported_sentences_are_removed() -> None:
    draft = "Ueno Bank was a Paraguayan bank [F1]. It collapsed because of fraud [F1]."
    run, llm = await run_agent(
        [response("", [call("read_page", title="Ueno Bank [F1]")]), draft, verdicts(True, False)],
        verify=True,
    )
    check = llm.requests[-1]
    assert check.system == load_prompt(VERIFY_PROMPT_VERSION) and check.tools == []
    assert check.think and check.json_schema is not None
    assert len(check.messages) == 1  # a fresh conversation, not the agent's
    assert "Ueno Bank was a Paraguayan bank." in check.messages[0].content  # the page text
    assert run.unsupported == ["It collapsed because of fraud [F1]."]
    assert run.briefing.text == "Ueno Bank was a Paraguayan bank [1]."
    assert run.briefing.removed_sentences == ["It collapsed because of fraud [F1]."]
    assert [s.kind for s in run.steps][-1] == "verify"
    # The model wrote the title with its source id; read_page still found the page.
    read = next(s for s in run.steps if s.tool == "read_page")
    assert not read.is_error


# In the fixture event, [F3] is the event's title and description (the extraction).
CHECKED = "Ueno Bank collapsed [F3]."


async def test_a_check_that_rejects_everything_leaves_no_briefing() -> None:
    run, _ = await run_agent([CHECKED, verdicts(False)], verify=True)
    assert run.briefing is None and run.unsupported == [CHECKED]


async def test_a_check_without_verdicts_publishes_nothing() -> None:
    run, _ = await run_agent([CHECKED, json.dumps({"verdicts": []})], verify=True)
    assert run.briefing is None and run.unsupported == [CHECKED]


async def test_a_citation_of_an_unread_page_never_reaches_the_check() -> None:
    draft = "Ueno Bank was a big bank [F1]. Ueno Bank collapsed [F3]."
    run, llm = await run_agent([draft, verdicts(True)], verify=True)
    request = llm.requests[-1].messages[0].content
    assert "Sentence 1: Ueno Bank collapsed [F3]." in request and "big bank" not in request
    assert run.briefing.text == "Ueno Bank collapsed [1]."
    assert run.briefing.removed_sentences == ["Ueno Bank was a big bank."]


async def test_the_description_is_cited_as_the_extraction() -> None:
    run, _ = await run_agent([CHECKED, verdicts(True)], verify=True)
    assert run.briefing.as_text().endswith(
        "Sources:\n[1] WikiPulse event run:7: title and description "
        "(written by WikiPulse's extraction model, not a Wikipedia text)"
    )


async def test_a_broken_check_keeps_the_briefing_marked_unchecked() -> None:
    run, llm = await run_agent([CHECKED, "not json", "still not"], verify=True)
    assert run.briefing is not None and run.unsupported == []
    assert run.verified is False and run.briefing.verified is False
    assert UNVERIFIED in run.briefing.as_text()
    assert llm.requests[-1].max_tokens == VERIFY_MAX_TOKENS
    timed_out, _ = await run_agent([CHECKED, DOWN], verify=True)
    assert timed_out.briefing.verified is False and timed_out.stop_reason == "answered"


async def test_a_checked_briefing_is_marked_verified() -> None:
    run, _ = await run_agent([CHECKED, verdicts(True)], verify=True)
    assert run.briefing.verified is True and UNVERIFIED not in run.briefing.as_text()

"""The grounding check: each sentence against the lines its source ids point to."""

import json

from tests.agent.helpers import DOWN, call, response, run_agent
from wikipulse.agent.briefing import (
    UNVERIFIED,
    evidence,
    unsupported_sentences,
    verification_request,
)
from wikipulse.agent.loop import VERIFY_MAX_TOKENS, VERIFY_PROMPT_VERSION, load_prompt

TOOL_TEXTS = [
    "Event run:7: Ueno Bank collapses [F3]\nWikipedia pages: Ueno Bank [F1]; Santiago Peña [F2]\n"
    "- Santiago Peña resigned as president of Paraguay. [F2]",
    "Wikipedia page 'Ueno Bank' [F1] (revision 101)\n<untrusted source=\"Ueno Bank\">\n"
    "Ueno Bank was a Paraguayan bank.\n</untrusted>",
]


def verdicts(*supported: bool) -> str:
    return json.dumps(
        {"verdicts": [{"n": n, "supported": s} for n, s in enumerate(supported, start=1)]}
    )


def test_evidence_is_the_lines_and_pages_behind_the_ids() -> None:
    assert evidence(["F2"], TOOL_TEXTS) == [
        "Wikipedia pages: Ueno Bank [F1]; Santiago Peña [F2]",
        "- Santiago Peña resigned as president of Paraguay. [F2]",
    ]
    found = evidence(["F1"], TOOL_TEXTS)
    assert found[-1] == TOOL_TEXTS[1]  # the whole page read
    assert evidence(["F9"], TOOL_TEXTS) == []


def test_the_request_numbers_cited_sentences() -> None:
    text = "Ueno Bank collapsed [F3]. Peña resigned [F2]."
    request, sentences = verification_request(text, TOOL_TEXTS)
    assert sentences == ["Ueno Bank collapsed [F3].", "Peña resigned [F2]."]
    assert request.startswith("Sentence 1: Ueno Bank collapsed [F3].\nEvidence for sentence 1:\n")
    assert "Sentence 2: Peña resigned [F2].\nEvidence for sentence 2:\n" in request
    assert unsupported_sentences(json.loads(verdicts(True, False)), sentences) == sentences[1:]
    assert unsupported_sentences({"verdicts": [{"n": 9, "supported": False}]}, sentences) == []
    assert unsupported_sentences(None, sentences) == []


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


async def test_a_check_that_rejects_everything_leaves_no_briefing() -> None:
    run, _ = await run_agent(["Ueno Bank collapsed [F1].", verdicts(False)], verify=True)
    assert run.briefing is None and run.unsupported == ["Ueno Bank collapsed [F1]."]


async def test_a_broken_check_keeps_the_briefing_marked_unchecked() -> None:
    run, llm = await run_agent(["Ueno Bank collapsed [F1].", "not json", "still not"], verify=True)
    assert run.briefing is not None and run.unsupported == []
    assert run.verified is False and run.briefing.verified is False
    assert UNVERIFIED in run.briefing.as_text()
    assert llm.requests[-1].max_tokens == VERIFY_MAX_TOKENS
    timed_out, _ = await run_agent(["Ueno Bank collapsed [F1].", DOWN], verify=True)
    assert timed_out.briefing.verified is False and timed_out.stop_reason == "answered"


async def test_a_checked_briefing_is_marked_verified() -> None:
    run, _ = await run_agent(["Ueno Bank collapsed [F1].", verdicts(True)], verify=True)
    assert run.briefing.verified is True and UNVERIFIED not in run.briefing.as_text()

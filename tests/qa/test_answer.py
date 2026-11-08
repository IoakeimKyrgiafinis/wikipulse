"""Grounded answers: fact ids become citations, uncited claims are caught (mocked LLM)."""

import json

from tests.llm.helpers import ScriptedClient
from wikipulse.qa.answer import (
    NO_FACTS,
    NOTHING_CITED,
    Answerer,
    build_message,
    cited_ids,
    drop_sentences,
    drop_unknown_ids,
    uncited_sentences,
)
from wikipulse.qa.ask import render
from wikipulse.qa.types import Citation, Context, Fact

PENA = Citation(title="Santiago Peña", revision=102)
UENO = Citation(title="Ueno Bank", revision=101)


def context() -> Context:
    ctx = Context(question="How are Ueno Bank and Santiago Peña connected?")
    ctx.add(Fact(kind="path", channel="graph", text="Connection ...", sources=[UENO, PENA]))
    ctx.add(Fact(kind="relation", channel="graph", text="Ueno Bank — collapsed", sources=[UENO]))
    ctx.add(Fact(kind="query", channel="cypher", text="Query result: x = 1", sources=[]))
    ctx.notes.append("vector: nothing similar enough")
    return ctx


def reply(answer: str, answerable: bool = True) -> str:
    return json.dumps({"answerable": answerable, "answer": answer})


def test_fact_ids_become_numbered_citations_of_page_revisions() -> None:
    client = ScriptedClient(
        [reply("Both are part of the bank collapse story [F1]. Ueno Bank collapsed [F2][F1].")]
    )
    result = Answerer(client).answer(context())
    assert result.answerable
    assert result.citations == [UENO, PENA]
    assert (
        result.text
        == "Both are part of the bank collapse story [1][2]. Ueno Bank collapsed [1][2]."
    )
    assert result.fact_text.endswith("[F2][F1].")
    assert [fact.id for fact in result.facts] == ["F1", "F2"]
    assert result.uncited_sentences == []
    assert len(client.requests) == 1


def test_the_model_sees_numbered_facts_and_the_notes_as_non_facts() -> None:
    message = build_message(context())
    assert "[F1] Connection ..." in message and "[F3] Query result: x = 1" in message
    assert "Search notes (not facts, do not cite):\n- vector: nothing similar enough" in message


def test_invented_fact_ids_are_dropped() -> None:
    assert drop_unknown_ids("A [F1][F9]. B [F9, F2].", {"F1", "F2"}) == "A [F1]. B [F2]."
    assert cited_ids("x [F2][F5, F1] y [F2]") == ["F2", "F5", "F1"]


def test_uncited_claims_get_one_retry_with_the_sentences_named() -> None:
    client = ScriptedClient(
        [
            reply("Ueno Bank collapsed [F2]. Peña was a famous economist."),
            reply("Ueno Bank collapsed [F2]."),
        ]
    )
    result = Answerer(client).answer(context())
    assert len(client.requests) == 2
    retry = client.requests[1].messages[-1].content
    assert "- Peña was a famous economist." in retry
    assert result.text == "Ueno Bank collapsed [1]." and result.uncited_sentences == []


def test_claims_still_uncited_after_the_retry_are_removed_and_reported() -> None:
    answer = "Ueno Bank collapsed [F2]. Peña was a famous economist."
    result = Answerer(ScriptedClient([reply(answer), reply(answer)])).answer(context())
    assert result.text == "Ueno Bank collapsed [1]."
    assert result.fact_text == "Ueno Bank collapsed [F2]."
    assert result.removed_sentences == ["Peña was a famous economist."]
    assert result.uncited_sentences == []
    shown = render(result)
    assert "Removed from the answer" in shown and "- Peña was a famous economist." in shown


def test_short_claims_and_list_items_must_be_cited_too() -> None:
    assert uncited_sentences("Peña resigned. Bank failed [F1].") == ["Peña resigned."]
    assert uncited_sentences("Organizations:\n- Ueno Bank\n- Democratic Party [F1]") == [
        "Ueno Bank"
    ]
    assert uncited_sentences("1. Ueno Bank\n2. Peña [F2]") == ["Ueno Bank"]


def test_uncited_short_claims_and_list_items_are_removed_after_the_retry() -> None:
    answer = "Organizations:\n- Ueno Bank\n- Democratic Party [F2]\n\nPeña resigned. Done [F1]."
    client = ScriptedClient([reply(answer), reply(answer)])
    result = Answerer(client).answer(context())
    retry = client.requests[1].messages[-1].content
    assert "- Ueno Bank" in retry and "- Peña resigned." in retry
    assert result.fact_text == "Organizations:\n- Democratic Party [F2]\n\nDone [F1]."
    assert result.removed_sentences == ["Ueno Bank", "Peña resigned."]


def test_a_list_cut_at_a_limit_is_told_to_the_model_and_the_user() -> None:
    ctx = context()
    ctx.limits.append("The query results stop at the limit of 25 rows; there may be more.")
    message = build_message(ctx)
    assert "Incomplete lists (do not present these lists as complete):\n- The query" in message
    result = Answerer(ScriptedClient([reply("Ueno Bank collapsed [F2].")])).answer(ctx)
    assert result.text == (
        "Ueno Bank collapsed [1].\n\n"
        "Note: The query results stop at the limit of 25 rows; there may be more."
    )
    assert result.limits == ctx.limits
    assert "there may be more" in render(result)  # without --show-context


def test_drop_sentences_keeps_bullets_and_the_rest_of_a_line() -> None:
    text = "Intro [F1].\n- A [F1]. B.\n- C.\n\n\n1. D [F2]"
    assert drop_sentences(text, ["B.", "C."]) == "Intro [F1].\n- A [F1].\n\n1. D [F2]"


def test_an_answer_that_cites_nothing_becomes_a_refusal() -> None:
    answer = "They are connected through a bank."
    result = Answerer(ScriptedClient([reply(answer), reply(answer)])).answer(context())
    assert not result.answerable
    assert result.text == NOTHING_CITED and result.citations == []


def test_unanswerable_questions_get_no_citations() -> None:
    client = ScriptedClient([reply("The knowledge graph has no facts about this [F1].", False)])
    result = Answerer(client).answer(context())
    assert not result.answerable
    assert result.text == "The knowledge graph has no facts about this." and not result.citations


def test_no_facts_means_no_model_call() -> None:
    client = ScriptedClient([])
    result = Answerer(client).answer(Context(question="Who won Eurovision?"))
    assert result.text == NO_FACTS and not result.answerable and client.requests == []


def test_headings_and_short_fragments_are_not_claims() -> None:
    text = "Organizations in several events:\n- Democratic Party [F1]\n- Ueno Bank, a bank [F2]"
    assert uncited_sentences(text) == []
    assert uncited_sentences("Yes.\nThe bank had two million customers.") == [
        "The bank had two million customers."
    ]
    # "U.S." does not end a sentence.
    assert uncited_sentences("Today the U.S. House elections were held [F1].") == []


def test_the_cli_shows_sources_with_links() -> None:
    client = ScriptedClient([reply("Ueno Bank collapsed [F2].")])
    text = render(Answerer(client).answer(context()), show_context=True)
    assert "[1] Ueno Bank, revision 101  https://en.wikipedia.org/w/index.php?oldid=101" in text
    assert "F2 (graph) Ueno Bank — collapsed" in text
    assert "Note: vector: nothing similar enough" in text


def test_a_sentence_citing_only_a_fact_without_sources_is_reported() -> None:
    client = ScriptedClient([reply("Ueno Bank collapsed [F2]. The value of x is one [F3].")])
    result = Answerer(client).answer(context())
    assert result.text == "Ueno Bank collapsed [1]. The value of x is one."
    assert result.uncited_sentences == ["The value of x is one."]

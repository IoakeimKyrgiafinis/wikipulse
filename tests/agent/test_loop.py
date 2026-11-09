"""The agent loop with a scripted LLM: tool calls, controls, the final no-tools answer
and the checks on the draft. The MCP server is the real one (in-process) on a fixture
graph, unless a test needs a tool that misbehaves."""

import json

from tests.agent.helpers import (
    DOWN,
    FakeMcp,
    ScriptedLLM,
    build_agent,
    call,
    fixture_graph,
    last_tool_text,
    response,
    run_agent,
)
from wikipulse.agent.loop import FINAL_PROMPT, PROMPT_VERSION, REASONS, load_prompt
from wikipulse.agent.tools import ToolLimits
from wikipulse.agent.transcript import save_transcript, transcript_name

ANSWER = "Ueno Bank collapsed [F1]. Santiago Peña resigned [F2]."


def tools_of(request) -> list[str]:
    return [tool.name for tool in request.tools]


async def test_the_model_calls_a_tool_then_writes_a_cited_briefing() -> None:
    run, llm = await run_agent([response("", [call("read_page", title="Ueno Bank")]), ANSWER])
    assert run.stop_reason == "answered" and run.error is None
    assert run.event_key == "run:7"
    assert [c.title for c in run.briefing.citations] == ["Ueno Bank", "Santiago Peña"]
    assert run.briefing.text == "Ueno Bank collapsed [1]. Santiago Peña resigned [2]."
    assert [(s.kind, s.tool) for s in run.steps] == [
        ("tool", "get_event"),  # the code's first call
        ("model", None),
        ("tool", "read_page"),
        ("model", None),
    ]
    assert run.model_turns == 2 and run.tool_calls == 2 and run.tokens == 220
    first = llm.requests[0]
    assert first.system == load_prompt(PROMPT_VERSION)
    assert tools_of(first) == [
        "get_event",
        "ask",
        "find_connection",
        "search_entities",
        "read_page",
        "related_events",
    ]
    # The code's get_event call is in the conversation like one of the model's.
    assert first.messages[1].tool_calls[0].name == "get_event"
    assert "Ueno Bank [F1]" in first.messages[2].content
    assert '<untrusted source="Ueno Bank">' in last_tool_text(llm.requests[1])
    assert run.messages[-1].content == ANSWER and run.sources["F1"] == ["Ueno Bank, revision 101"]


async def test_max_steps_ends_with_a_final_answer_without_tools() -> None:
    run, llm = await run_agent(
        [
            response("", [call("read_page", title="Ueno Bank")]),
            response("", [call("read_page", title="Santiago Peña")]),
            ANSWER,
        ],
        max_steps=2,
    )
    assert run.stop_reason == "max_steps" and run.briefing is not None
    final = llm.requests[-1]
    assert final.tools == []
    assert final.messages[-1].content == FINAL_PROMPT.format(reason=REASONS["max_steps"])
    assert [s.kind for s in run.steps if s.kind != "tool"] == ["model", "model", "final"]


async def test_the_token_budget_stops_the_investigation() -> None:
    run, llm = await run_agent(
        [
            response("", [call("read_page", title="Ueno Bank")]),
            response("", [call("read_page", title="Santiago Peña")]),
            ANSWER,
        ],
        token_budget=150,  # each scripted call costs 110 tokens
    )
    assert run.stop_reason == "token_budget" and run.model_turns == 2
    assert llm.requests[-1].tools == [] and run.briefing is not None


async def test_the_same_call_three_times_is_a_loop() -> None:
    same = call("read_page", title="Ueno Bank")
    run, llm = await run_agent(
        [response("", [same]), response("", [same]), response("", [same]), ANSWER]
    )
    assert run.stop_reason == "loop_detected"
    reads = [s for s in run.steps if s.tool == "read_page"]
    assert [s.repeated for s in reads] == [0, 1, 2]
    assert reads[1].text.startswith("You already called read_page with exactly these arguments")
    assert "Ueno Bank was a Paraguayan bank" in reads[1].text  # the earlier result, not run again
    assert reads[2].text.startswith("Not run: this is call number 3 of read_page")
    assert llm.requests[-1].tools == [] and run.briefing is not None


async def test_argument_order_does_not_hide_a_repeat() -> None:
    a = call("find_connection", entity_a="Ueno Bank", entity_b="Santiago Peña")
    b = call("find_connection", entity_b="Santiago Peña", entity_a="Ueno Bank")
    run, _ = await run_agent([response("", [a]), response("", [b]), ANSWER])
    assert [s.repeated for s in run.steps if s.tool == "find_connection"] == [0, 1]


async def test_a_slow_tool_times_out_and_the_run_goes_on() -> None:
    mcp = FakeMcp(delays={"ask": 5})
    # F2 is the event's title (FakeMcp's event has no relations); F1 was never read.
    llm = ScriptedLLM([response("", [call("ask", question="Why?")]), "Ueno Bank collapsed [F2]."])
    agent = build_agent(
        llm, mcp, fixture_graph(), tool_limits=ToolLimits(timeout_s=5, ask_timeout_s=0.05)
    )
    run = await agent.run("run:7")
    ask = next(s for s in run.steps if s.tool == "ask")
    assert ask.timed_out and ask.is_error and ask.seconds < 2
    assert ask.text.startswith("Error: ask did not answer within 0.05 seconds")
    assert mcp.calls[1][2] == 0.05  # the timeout also goes to the MCP request
    assert run.stop_reason == "answered" and run.briefing is not None


async def test_a_slow_local_tool_times_out(monkeypatch) -> None:
    import time

    graph = fixture_graph()
    slow = graph.page_text

    def page_text(title, event_key=None):
        time.sleep(0.5)
        return slow(title, event_key)

    monkeypatch.setattr(graph, "page_text", page_text)
    run, _ = await run_agent(
        [response("", [call("read_page", title="Ueno Bank")]), "Ueno Bank collapsed [F1]."],
        graph=graph,
        tool_limits=ToolLimits(timeout_s=0.05),
    )
    read = next(s for s in run.steps if s.tool == "read_page")
    assert read.timed_out and read.seconds < 0.4


async def test_bad_calls_become_error_results_the_model_can_read() -> None:
    run, llm = await run_agent(
        [
            response(
                "",
                [
                    call("delete_everything", 0),
                    call("read_page", 1),
                    call("read_page", 2, title="No Such Page"),
                    call("get_event", 3, event_id="Ueno Bank"),
                ],
            ),
            ANSWER,
        ]
    )
    texts = [s.text for s in run.steps if s.kind == "tool"][1:]
    assert texts[0].startswith("Error: there is no tool 'delete_everything'. Use one of:")
    assert texts[1] == 'Error: read_page needs a text argument \'title\', e.g. {"title": "..."}.'
    assert texts[2].startswith("Error: There is no page 'No Such Page' in the graph.")
    # The MCP server's own error message, which says what to do instead.
    assert texts[3].startswith("Error executing tool get_event: 'Ueno Bank' is not an event id.")
    assert run.briefing is not None


async def test_at_most_four_calls_per_turn() -> None:
    calls = [call("read_page", n, title=f"Page {n}") for n in range(5)]
    run, _ = await run_agent([response("", calls), ANSWER])
    texts = [s.text for s in run.steps if s.tool == "read_page"]
    assert texts[-1] == "Not run: too many calls in one turn."
    assert all(not t.startswith("Not run") for t in texts[:4])


async def test_an_uncited_sentence_gets_one_retry_then_goes() -> None:
    draft = "Ueno Bank collapsed [F3]. It was a shock."
    run, llm = await run_agent([draft, draft])
    retry = llm.requests[-1]
    assert retry.tools == []
    assert "- It was a shock." in retry.messages[-1].content
    assert run.briefing.text == "Ueno Bank collapsed [1]."
    assert run.briefing.removed_sentences == ["It was a shock."]
    assert [s.kind for s in run.steps if s.kind != "tool"] == ["model", "retry"]


async def test_a_draft_without_any_valid_id_is_no_briefing() -> None:
    run, _ = await run_agent(["The bank collapsed [F42].", "The bank collapsed."])
    assert run.briefing is None
    assert run.error.startswith("No sentence of the draft passed the checks")


async def test_links_in_the_draft_never_reach_the_briefing() -> None:
    run, _ = await run_agent(["Ueno Bank collapsed [F3]; see https://example.com/x [F3]."])
    assert "example.com" not in run.briefing.as_text()
    assert run.briefing.removed_links == ["https://example.com/x"]


async def test_an_unknown_event_ends_the_run_before_any_model_call() -> None:
    run, llm = await run_agent([], event="run:404")
    assert run.stop_reason == "event_not_found" and llm.requests == []
    assert "There is no event 'run:404'" in run.error


async def test_a_failing_model_ends_the_run() -> None:
    run, _ = await run_agent([DOWN])
    assert run.stop_reason == "llm_error" and run.briefing is None
    assert "not reachable" in run.error


async def test_the_context_window_guard() -> None:
    big = response("", [call("read_page", title="Ueno Bank")], tokens=900)
    run, llm = await run_agent([big, ANSWER], context_tokens=1000)
    assert run.stop_reason == "context_full" and llm.requests[-1].tools == []


async def test_the_run_timeout() -> None:
    run, llm = await run_agent([ANSWER], run_timeout_s=0)
    assert run.stop_reason == "timeout" and len(llm.requests) == 1
    assert llm.requests[0].tools == []


async def test_an_empty_answer_gets_the_final_no_tools_turn() -> None:
    run, llm = await run_agent(["", ANSWER])
    assert run.stop_reason == "empty_answer" and run.briefing is not None
    assert llm.requests[-1].tools == []


async def test_the_transcript_is_saved_as_json(tmp_path) -> None:
    run, _ = await run_agent([ANSWER])
    path = save_transcript(run, tmp_path / "agent_runs")
    assert path.name == transcript_name(run) and path.name.startswith("run-7_")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["event_key"] == "run:7" and data["stop_reason"] == "answered"
    assert data["system_prompt"].startswith("You are the WikiPulse analyst")
    assert [m["role"] for m in data["messages"]] == ["user", "assistant", "tool", "assistant"]
    # Ueno Bank [F1] was never read: that sentence went, Peña's relation line backs [F2].
    assert data["briefing"]["citations"][0]["title"] == "Santiago Peña"
    assert data["briefing"]["removed_sentences"] == ["Ueno Bank collapsed."]


async def test_page_reads_cite_the_revision_of_the_event_being_briefed() -> None:
    from tests.agent.helpers import EVENT, OTHER
    from tests.agent.test_tools import LATER

    run, _ = await run_agent(
        [response("", [call("read_page", title="Ueno Bank")]), ANSWER],
        graph=fixture_graph(EVENT, OTHER, LATER),  # LATER has Ueno Bank at revision 201
    )
    assert run.sources["F1"] == ["Ueno Bank, revision 101"]
    assert [c.revision for c in run.briefing.citations] == [101, 102]

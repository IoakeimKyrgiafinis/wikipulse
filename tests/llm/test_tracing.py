"""LLM and agent tracing (spec 09b): it works with keys and does nothing without them.

"With keys" uses the real Langfuse SDK with an in-memory OpenTelemetry exporter instead
of the network: the spans it would send are inspected here. Nothing leaves the process.
"""

import asyncio
import itertools
import json
import logging
import sys
import threading
import time
from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.llm.helpers import ScriptedClient
from wikipulse.config import Settings
from wikipulse.llm import Message, NoopTracer, build_tracer, set_tracer, trace
from wikipulse.llm.tracing import NOOP_OBSERVATION, get_tracer, tracing_configured
from wikipulse.logging_setup import current_context, log_context

# The SDK keeps one client per public key, so every test gets its own key.
_keys = itertools.count()


def settings(**values: str) -> Settings:
    return Settings(_env_file=None, **values)


# --- without keys ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "keys",
    [
        {},
        {"langfuse_public_key": "pk-lf-1"},
        {"langfuse_secret_key": "sk-lf-1"},
        {"langfuse_public_key": " ", "langfuse_secret_key": "sk-lf-1"},
    ],
)
def test_without_both_keys_tracing_is_a_no_op(keys: dict[str, str]) -> None:
    assert not tracing_configured(settings(**keys))
    assert isinstance(build_tracer(settings(**keys)), NoopTracer)


def test_without_keys_the_sdk_is_never_imported(monkeypatch: pytest.MonkeyPatch) -> None:
    # Any import of langfuse (or of our module that imports it) would now fail.
    monkeypatch.setitem(sys.modules, "langfuse", None)
    monkeypatch.delitem(sys.modules, "wikipulse.llm.langfuse_tracing", raising=False)
    set_tracer(None)
    monkeypatch.setattr("wikipulse.llm.tracing.get_settings", lambda: settings())
    client = ScriptedClient(["hello"])
    with log_context(correlation_id="edit-1"), trace("agent run") as observed:
        assert observed is NOOP_OBSERVATION
        observed.update(output="anything", error="ignored")
        answer = client.chat("hi")
    assert answer.text == "hello"
    assert isinstance(get_tracer(), NoopTracer)
    assert "trace_id" not in current_context()


def test_threads_racing_for_the_tracer_build_it_once(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[NoopTracer] = []
    started = threading.Barrier(8)

    def slow_build(_settings: Settings) -> NoopTracer:
        time.sleep(0.05)  # long enough for every thread to arrive while it builds
        built.append(NoopTracer())
        return built[-1]

    set_tracer(None)
    monkeypatch.setattr("wikipulse.llm.tracing.get_settings", lambda: settings())
    monkeypatch.setattr("wikipulse.llm.tracing.build_tracer", slow_build)
    tracers: list[object] = []

    def worker() -> None:
        started.wait()
        tracers.append(get_tracer())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    set_tracer(None)
    assert len(built) == 1 and all(tracer is built[0] for tracer in tracers)


def test_the_noop_tracer_lets_errors_through() -> None:
    with pytest.raises(ValueError), trace("step"):
        raise ValueError("not swallowed")


# --- with keys ---------------------------------------------------------------------------


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    """A real Langfuse tracer whose spans land in memory."""
    from wikipulse.llm.langfuse_tracing import LangfuseTracer

    spans = InMemorySpanExporter()
    n = next(_keys)
    tracer = LangfuseTracer.connect(
        f"pk-lf-test-{n}",
        f"sk-lf-test-{n}",
        "http://127.0.0.1:9",  # unreachable on purpose; the exporter replaces the network
        span_exporter=spans,
        tracer_provider=TracerProvider(),
    )
    set_tracer(tracer)
    yield spans
    tracer.flush()
    tracer.client.shutdown()


def finished(spans: InMemorySpanExporter) -> dict[str, dict]:
    get_tracer().flush()
    return {span.name: dict(span.attributes or {}) for span in spans.get_finished_spans()}


def test_build_tracer_with_both_keys_connects(monkeypatch: pytest.MonkeyPatch) -> None:
    from wikipulse.llm.langfuse_tracing import LangfuseTracer

    calls = []
    monkeypatch.setattr(
        LangfuseTracer, "connect", classmethod(lambda cls, *args: calls.append(args) or "t")
    )
    values = {
        "langfuse_public_key": "pk-lf-1",
        "langfuse_secret_key": "sk-lf-1",
        "langfuse_host": "http://langfuse.local:3000",
    }
    assert build_tracer(settings(**values)) == "t"
    assert calls == [("pk-lf-1", "sk-lf-1", "http://langfuse.local:3000")]


def test_a_chat_becomes_a_generation_in_the_story_session(
    exporter: InMemorySpanExporter,
) -> None:
    client = ScriptedClient(["Paris"], model="qwen3:8b")
    with log_context(correlation_id="edit-7"), trace("agent run", as_type="agent"):
        client.chat([Message.user("Capital of France?")], system="Be brief.", max_tokens=50)
    spans = finished(exporter)
    run, generation = spans["agent run"], spans["fake chat"]
    assert run["session.id"] == "edit-7" and generation["session.id"] == "edit-7"
    assert run["langfuse.observation.type"] == "agent"
    assert generation["langfuse.observation.type"] == "generation"
    assert generation["langfuse.observation.model.name"] == "qwen3:8b"
    assert json.loads(generation["langfuse.observation.input"]) == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Capital of France?"},
    ]
    assert generation["langfuse.observation.output"] == "Paris"
    assert json.loads(generation["langfuse.observation.usage_details"]) == {
        "input": 10,
        "output": 5,
    }
    parameters = generation["langfuse.observation.model.parameters"]
    assert '"max_tokens": 50' in parameters


def test_steps_nest_in_one_trace(exporter: InMemorySpanExporter) -> None:
    with trace("agent run", as_type="agent"), trace("tool get_event", as_type="tool") as step:
        step.update(output="the event")
        ScriptedClient(["ok"]).chat("next?")
    get_tracer().flush()
    finished_spans = exporter.get_finished_spans()
    assert len({span.context.trace_id for span in finished_spans}) == 1
    by_name = {span.name: span for span in finished_spans}
    assert by_name["fake chat"].parent.span_id == by_name["tool get_event"].context.span_id
    assert by_name["tool get_event"].parent.span_id == by_name["agent run"].context.span_id


def test_a_failed_call_is_recorded_as_an_error(exporter: InMemorySpanExporter) -> None:
    class Broken(ScriptedClient):
        def _send(self, request):  # noqa: ANN001, ANN202
            raise ConnectionError("Ollama is down")

    with pytest.raises(ConnectionError), trace("graph extraction", correlation_id="e-9"):
        Broken([]).chat("hi")
    spans = finished(exporter)
    assert spans["fake chat"]["langfuse.observation.level"] == "ERROR"
    assert "Ollama is down" in spans["fake chat"]["langfuse.observation.status_message"]
    assert spans["graph extraction"]["langfuse.observation.level"] == "ERROR"
    assert spans["graph extraction"]["session.id"] == "e-9"


def test_log_lines_inside_a_trace_carry_its_id(exporter: InMemorySpanExporter) -> None:
    with trace("agent run"):
        trace_id = current_context()["trace_id"]
        logging.getLogger("test.trace").warning("inside")
    assert len(trace_id) == 32
    assert "trace_id" not in current_context()
    get_tracer().flush()
    run = exporter.get_finished_spans()[0]
    assert format(run.context.trace_id, "032x") == trace_id


def test_the_context_follows_the_agent_into_threads(exporter: InMemorySpanExporter) -> None:
    client = ScriptedClient(["a"])

    async def run() -> None:
        with trace("agent run", as_type="agent", correlation_id="edit-5"):
            await asyncio.to_thread(client.chat, "hi")

    asyncio.run(run())
    spans = finished(exporter)
    assert spans["fake chat"]["session.id"] == "edit-5"

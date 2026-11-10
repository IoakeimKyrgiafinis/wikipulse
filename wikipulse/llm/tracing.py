"""LLM and agent tracing (spec 09b): Langfuse when keys are set, a silent no-op otherwise.

    from wikipulse.llm import trace

    with log_context(correlation_id=cid), trace("graph.extract", input={"event": key}):
        llm.chat(...)   # recorded inside the trace as a generation

    with trace("tool get_event", as_type="tool", input=arguments) as step:
        ...
        step.update(output=text)

- `trace()` opens an observation: the first one without a trace around it starts a new
  trace, the ones inside become its children (a tree: run -> steps -> LLM calls).
- `LLMClient._ask()` records every request as a *generation*: model, messages, answer,
  tokens, latency, errors. So every caller, extraction, questions, the agent, is traced
  in one place, as with the metrics of 09a.
- A new trace takes its session from the bound correlation id (`log_context`), so in
  Langfuse every trace of one story (the extraction, each briefing run) is one session.
  The other way round, log lines written inside a trace carry its `trace_id`.
- Without LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY, `get_tracer()` is a `NoopTracer`:
  the `langfuse` package is never imported and nothing is sent anywhere.

Callers only see this module; the Langfuse SDK is used in `langfuse_tracing.py` alone.
"""

import logging
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any, Literal, Protocol

from wikipulse.config import Settings, get_settings
from wikipulse.llm.types import Usage
from wikipulse.logging_setup import current_context

logger = logging.getLogger(__name__)

# The observation types WikiPulse uses (Langfuse shows each with its own icon).
Kind = Literal["span", "agent", "tool", "chain", "generation"]


class Observation(Protocol):
    """One step being traced; `update()` adds what is only known at its end."""

    def update(
        self,
        *,
        output: Any = None,
        usage: Usage | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...


class Tracer(Protocol):
    """Where observations go. `enabled` is False for the no-op."""

    enabled: bool

    def observe(
        self,
        name: str,
        *,
        as_type: Kind = "span",
        input: Any = None,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
        model: str | None = None,
        model_parameters: dict[str, Any] | None = None,
    ) -> AbstractContextManager[Observation]: ...

    def flush(self) -> None: ...


class NoopObservation:
    """Accepts every update and keeps nothing."""

    def update(
        self,
        *,
        output: Any = None,
        usage: Usage | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        return None


NOOP_OBSERVATION = NoopObservation()


class NoopTracer:
    """No keys, no tracing: every observation is the same do-nothing object."""

    enabled = False

    @contextmanager
    def observe(self, name: str, **_: Any) -> Iterator[Observation]:
        yield NOOP_OBSERVATION

    def flush(self) -> None:
        return None


def tracing_configured(settings: Settings) -> bool:
    """True if both Langfuse keys are set (and not empty)."""
    keys = (settings.langfuse_public_key, settings.langfuse_secret_key)
    return all(key is not None and key.get_secret_value().strip() for key in keys)


def build_tracer(settings: Settings) -> Tracer:
    """A Langfuse tracer if both keys are set, else the no-op."""
    if not tracing_configured(settings):
        return NoopTracer()
    from wikipulse.llm.langfuse_tracing import LangfuseTracer  # only with keys

    assert settings.langfuse_public_key is not None and settings.langfuse_secret_key is not None
    tracer = LangfuseTracer.connect(
        settings.langfuse_public_key.get_secret_value(),
        settings.langfuse_secret_key.get_secret_value(),
        settings.langfuse_host,
    )
    logger.info("LLM tracing to Langfuse at %s", settings.langfuse_host)
    return tracer


_tracer: Tracer | None = None


def get_tracer() -> Tracer:
    """The process's tracer, built from the settings on first use."""
    global _tracer
    if _tracer is None:
        _tracer = build_tracer(get_settings())
    return _tracer


def set_tracer(tracer: Tracer | None) -> None:
    """Replace the process's tracer (tests); None builds it from the settings again."""
    global _tracer
    _tracer = tracer


def trace(
    name: str,
    *,
    as_type: Kind = "span",
    input: Any = None,
    metadata: dict[str, Any] | None = None,
    correlation_id: str | None = None,
) -> AbstractContextManager[Observation]:
    """An observation named `name` around a block (see the module docstring).

    The session of a new trace is `correlation_id`, or the one bound by `log_context()`.
    """
    session = correlation_id or current_context().get("correlation_id")
    return get_tracer().observe(
        name, as_type=as_type, input=input, metadata=metadata, session_id=session
    )

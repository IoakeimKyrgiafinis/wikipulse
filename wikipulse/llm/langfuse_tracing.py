"""The Langfuse tracer: the only module that imports the Langfuse SDK (spec 09b).

Langfuse's SDK is built on OpenTelemetry: an observation is an OTel span, the current
one lives in a context variable (so it follows `asyncio.to_thread` into worker threads),
and a background thread sends finished spans in batches. `build_tracer()` in
`tracing.py` imports this module only when both keys are set.
"""

import logging
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any

from langfuse import Langfuse, propagate_attributes
from opentelemetry import trace as otel_trace

from wikipulse.llm.tracing import Kind
from wikipulse.llm.types import Usage
from wikipulse.logging_setup import log_context

logger = logging.getLogger(__name__)


class LangfuseObservation:
    """Our `Observation` on top of one Langfuse observation."""

    def __init__(self, observation: Any) -> None:
        self.observation = observation

    def update(
        self,
        *,
        output: Any = None,
        usage: Usage | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        fields: dict[str, Any] = {}
        if output is not None:
            fields["output"] = output
        if metadata:
            fields["metadata"] = metadata
        if usage is not None:
            fields["usage_details"] = {
                "input": usage.input_tokens,
                "output": usage.output_tokens,
            }
        if error is not None:
            fields["level"] = "ERROR"
            fields["status_message"] = error
        if fields:
            self.observation.update(**fields)


class LangfuseTracer:
    """Sends observations to Langfuse through a `Langfuse` client."""

    enabled = True

    def __init__(self, client: Langfuse) -> None:
        self.client = client

    @classmethod
    def connect(
        cls, public_key: str, secret_key: str, host: str, **options: Any
    ) -> "LangfuseTracer":
        """A tracer with a new client; `options` go to `Langfuse()` (tests: an exporter)."""
        client = Langfuse(public_key=public_key, secret_key=secret_key, base_url=host, **options)
        return cls(client)

    @contextmanager
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
    ) -> Iterator[LangfuseObservation]:
        """One observation; a new trace if none is open. Errors are recorded, then raised."""
        is_root = not otel_trace.get_current_span().get_span_context().is_valid
        with ExitStack() as stack:
            observation = stack.enter_context(
                self.client.start_as_current_observation(
                    name=name,
                    as_type=as_type,
                    input=input,
                    metadata=metadata,
                    model=model,
                    model_parameters=model_parameters,
                )
            )
            if is_root and session_id:
                # Trace-level: every observation of this trace belongs to the session.
                stack.enter_context(propagate_attributes(session_id=session_id))
            stack.enter_context(log_context(trace_id=self.client.get_current_trace_id()))
            wrapped = LangfuseObservation(observation)
            try:
                yield wrapped
            except BaseException as error:
                wrapped.update(error=f"{type(error).__name__}: {error}")
                raise

    def flush(self) -> None:
        """Send what is buffered now (the SDK also does it in the background and at exit)."""
        self.client.flush()

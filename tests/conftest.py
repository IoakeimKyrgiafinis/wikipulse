"""Shared test setup."""

from collections.abc import Iterator

import pytest

from wikipulse.llm import NoopTracer, set_tracer


@pytest.fixture(autouse=True)
def no_tracing() -> Iterator[None]:
    """Never trace to Langfuse from a test, even with keys in a developer's `.env`.

    Tests of tracing install their own tracer (tests/llm/test_tracing.py).
    """
    set_tracer(NoopTracer())
    yield
    set_tracer(None)

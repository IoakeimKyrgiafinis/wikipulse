"""Live test: prompt-injection cases against the local model (AGENT_MODEL, Ollama).

Run with `uv run pytest -m live tests/agent` (about a minute). The full set of cases with
a report is `uv run python -m wikipulse.agent.injection`.
"""

import pytest

from wikipulse.agent.fixtures import load_cases
from wikipulse.agent.injection import run_cases
from wikipulse.agent.loop import AgentLimits
from wikipulse.agent.tools import ToolLimits
from wikipulse.config import get_settings
from wikipulse.llm import LLMError, get_client

pytestmark = pytest.mark.live

CASE_IDS = ("ignore-instructions", "fake-system-message", "tool-hijack", "control-no-injection")


async def test_the_model_does_not_follow_planted_instructions() -> None:
    settings = get_settings()
    cases = [case for case in load_cases() if case.id in CASE_IDS]
    try:
        with get_client(settings.agent_model, settings) as client:
            limits = AgentLimits.from_settings(settings)
            results = await run_cases(client, cases, limits, ToolLimits())
    except LLMError as error:
        pytest.skip(f"the local model is not available: {error}")
    failed = {r.id: (r.leaked, r.tool_arg_leaks, r.stop_reason) for r in results if not r.passed}
    assert not failed

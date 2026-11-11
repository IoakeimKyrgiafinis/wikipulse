"""Agent transcripts: one JSON file per run in data/agent_runs/ (never committed).

A transcript is the whole `AgentRun`: the system prompt, every message sent to the
model, every tool call with its arguments and the result the model saw, token usage
and timings, the draft and the checked briefing. It is how you debug an agent: when a
briefing is wrong, read what the model was shown and what it did with it.
"""

import re
from pathlib import Path

from wikipulse.agent.loop import AgentRun

_UNSAFE = re.compile(r"[^\w.-]+")


def transcript_name(run: AgentRun) -> str:
    """e.g. `03b-default-fix-341_20261109T120000Z.json` (no ':' for Windows)."""
    event = _UNSAFE.sub("-", run.event_key or run.event_id).strip("-") or "event"
    return f"{event}_{run.started_at:%Y%m%dT%H%M%SZ}.json"


def save_transcript(run: AgentRun, directory: Path) -> Path:
    """Write the run's transcript as JSON into `directory`; returns its path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / transcript_name(run)
    path.write_text(run.model_dump_json(indent=2), encoding="utf-8")
    return path

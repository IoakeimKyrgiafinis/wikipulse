"""scripts/make_gate_fixture.py: which label a gate burst gets (spec 12b), no LLM, no data/."""

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.eval.helpers import spike
from wikipulse.eval.label_store import LabelRecord

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_gate_fixture.py"
spec = importlib.util.spec_from_file_location("make_gate_fixture", SCRIPT)
assert spec is not None and spec.loader is not None
make_gate_fixture = importlib.util.module_from_spec(spec)
sys.modules["make_gate_fixture"] = make_gate_fixture
spec.loader.exec_module(make_gate_fixture)

START = datetime(2026, 11, 7, 3, tzinfo=UTC)
LABELED_AT = datetime(2026, 11, 15, tzinfo=UTC)


def burst() -> "make_gate_fixture.Burst":
    return make_gate_fixture.bursts_of([spike("Page", START)])[0]


def label(value: str, labeler: str, run_id: str, model: str | None = None) -> LabelRecord:
    b = burst()
    return LabelRecord(
        title=b.title,
        run_id=run_id,
        started_at=START,
        first_edit_at=b.first_edit_at,
        end_at=b.end_at,
        label=value,
        labeler=labeler,
        model=model,
        labeled_at=LABELED_AT,
    )


def labels_for(sources: list[LabelRecord], kept: list[LabelRecord], **options: object) -> str:
    [record] = make_gate_fixture.label_bursts(
        [burst()], [], sources, kept, None, "ollama:qwen3:8b", **options
    )
    return f"{record.label} ({record.labeler})"


GATE_LLM = label("not news", "llm", "09c-gate", "ollama:qwen3:8b")
SOURCE_LLM = label("news", "llm", "07a-default", "ollama:qwen3:4b")
SOURCE_HUMAN = label("news", "human", "07a-candidates")


def test_a_human_spike_label_replaces_the_gate_llm_label() -> None:
    assert labels_for([SOURCE_LLM, SOURCE_HUMAN], [GATE_LLM]) == "news (human)"


def test_a_newer_llm_spike_label_does_not_replace_the_gate_llm_label() -> None:
    assert labels_for([SOURCE_LLM], [GATE_LLM]) == "not news (llm)"


def test_relabel_drops_the_kept_llm_label() -> None:
    assert labels_for([SOURCE_LLM], [GATE_LLM], relabel=True) == "news (llm)"


def test_a_human_label_in_the_gate_file_wins() -> None:
    gate_human = label("not news", "human", "09c-gate")
    assert labels_for([SOURCE_HUMAN], [GATE_LLM, gate_human]) == "not news (human)"


def test_without_any_label_and_without_the_llm_it_stops() -> None:
    with pytest.raises(SystemExit):
        labels_for([], [])


def test_a_new_llm_label_needs_the_recording_for_its_comments() -> None:
    class NeverAsked:
        def label(self, context: object) -> tuple[str, str]:
            raise AssertionError("the LLM must not be asked without edit comments")

    with pytest.raises(SystemExit, match="--file"):
        make_gate_fixture.label_bursts([burst()], [], [], [], NeverAsked(), "m")


def test_labels_only_needs_no_recording_or_times() -> None:
    args = make_gate_fixture.parse_args(["--labels-only", "--no-llm"])
    assert args.labels_only and args.file == []
    with pytest.raises(SystemExit):
        make_gate_fixture.parse_args(["--no-llm"])  # a full rebuild needs --file and times

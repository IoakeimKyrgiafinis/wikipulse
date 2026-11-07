"""The spike labels file, `evals/labels/spike_labels.jsonl` (one JSON record per line).

The file is append-only: a new judgement is a new line, nothing is ever rewritten, so
a human label can never be overwritten. When several labels cover the same spike, a
human label beats an LLM label and a later one beats an earlier one
(`metrics.pick_label`).
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel

from wikipulse.detect.detector import SpikeRecord
from wikipulse.eval.metrics import Label, spike_end

logger = logging.getLogger(__name__)

DEFAULT_LABELS_PATH = Path("evals") / "labels" / "spike_labels.jsonl"

LabelValue = Literal["news", "not news", "unsure"]
Labeler = Literal["llm", "human"]


class LabelRecord(BaseModel):
    """One line of the labels file."""

    title: str
    run_id: str  # the detector run the spike was sampled from
    started_at: AwareDatetime
    first_edit_at: AwareDatetime
    end_at: AwareDatetime  # ended_at, or the last edit if the spike was still active
    label: LabelValue
    labeler: Labeler
    model: str | None = None  # for LLM labels, e.g. "ollama:qwen3:8b"
    reason: str = ""
    labeled_at: AwareDatetime  # wall clock, metadata only

    def to_label(self) -> Label:
        return Label(
            self.title, self.first_edit_at, self.end_at, self.label, self.labeler, self.model
        )

    def is_for(self, spike: SpikeRecord) -> bool:
        return self.to_label().covers(spike)


def record_for(
    spike: SpikeRecord,
    run_id: str,
    label: LabelValue,
    labeler: Labeler,
    labeled_at: datetime,
    model: str | None = None,
    reason: str = "",
) -> LabelRecord:
    return LabelRecord(
        title=spike.title,
        run_id=run_id,
        started_at=spike.started_at,
        first_edit_at=spike.first_edit_at,
        end_at=spike_end(spike),
        label=label,
        labeler=labeler,
        model=model,
        reason=reason,
        labeled_at=labeled_at,
    )


def load_records(path: Path) -> list[LabelRecord]:
    """All records, oldest first. A missing file means no labels yet."""
    if not path.exists():
        return []
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(LabelRecord.model_validate_json(line))
        except ValueError as error:
            logger.warning("Skipping bad line %d of %s: %s", number, path, error)
    return records


def load_labels(path: Path) -> list[Label]:
    return [record.to_label() for record in load_records(path)]


def append_record(path: Path, record: LabelRecord) -> None:
    """Add one record at the end of the file (never rewrites existing lines)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record.model_dump(mode="json"), ensure_ascii=False)
    with path.open("a", encoding="utf-8", newline="\n") as out:
        out.write(line + "\n")

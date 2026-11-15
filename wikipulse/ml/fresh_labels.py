"""Fresh labels for the weekly retraining (spec 11b): the DAG's `label_new_spikes` task.

    python -m wikipulse.jobs.retrain label --date 2026-11-09 [--as-of 2026-11-15T18:00]

Before `build_dataset`, the task finds the spikes the build will turn into rows (the same
code: `wikipulse.ml.dataset.find_rows`) and asks the LLM spike labeler of spec 02b
(`wikipulse.eval.label`, through `wikipulse.llm`) about the ones nobody has judged yet:

- **Which spikes.** A spike needs a label when no label record covers it (human or LLM,
  any model, `unsure` included) and the "Current events" portal doesn't link it (a link
  already makes it news). So a second run labels nothing twice, and a human label is
  never touched: records are only ever appended to `evals/labels/spike_labels.jsonl`.
- **In which order.** First the days that can be test days at the as-of moment (portal
  final, label delay passed: `dataset.day_readiness`), newest first: without labels
  there, the build has to move the test split back. Then the newer days, oldest first:
  they are the next weeks' test days.
- **Within a budget.** At most `ML_RETRAIN_LABEL_BUDGET` spikes per run and
  `ML_RETRAIN_LABEL_MAX_MINUTES` minutes, with `ML_RETRAIN_LABEL_MODEL` (qwen3:4b, the
  model of every spike label so far). What doesn't fit waits for the next run.
- **Never fatal for missing labels.** If Ollama is down (or the model is missing), or
  Wikimedia's API fails, the task logs it, keeps the labels it has, and succeeds; the
  build then delays the days that aren't labeled enough.
"""

import logging
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol

import httpx

from wikipulse.detect.detector import SpikeRecord
from wikipulse.eval.ground_truth import PortalDay
from wikipulse.eval.label_store import LabelRecord, append_record, load_records, record_for
from wikipulse.llm import LLMError
from wikipulse.ml.dataset import day_of, day_readiness, portal_days_of

logger = logging.getLogger(__name__)

BUDGET, TIME, LLM_DOWN, WIKI_DOWN = "budget", "time", "llm unavailable", "wikimedia unavailable"


class Labeler(Protocol):
    """What labels one spike: `wikipulse.eval.label.LLMLabeler`, or a fake in tests."""

    def label(self, context: object) -> tuple[str, str]: ...


def needs_label(
    spike: SpikeRecord, records: Sequence[LabelRecord], portal: Sequence[PortalDay], as_of: datetime
) -> bool:
    """True if nobody has judged the spike: no label record of any labeler or model covers
    it, and the portal doesn't link it (a link makes it news without asking anyone)."""
    if portal_days_of(spike, portal, as_of):
        return False
    return not any(record.is_for(spike) for record in records)


def label_order(spikes: Sequence[SpikeRecord], testable: set[str]) -> list[SpikeRecord]:
    """The spikes in the order they are worth labeling.

    Days that can be test days now (`testable`) first, newest first: the test split is
    made of them, and an unlabeled one moves it back. Then the newer days, oldest first
    (the next to become testable). Within a day, by start time.
    """

    def key(spike: SpikeRecord) -> tuple[int, int, datetime, str]:
        day = date.fromisoformat(day_of(spike)).toordinal()
        group, order = (0, -day) if day_of(spike) in testable else (1, day)
        return group, order, spike.started_at, spike.title

    return sorted(spikes, key=key)


@dataclass
class LabelRun:
    """What one labeling run did."""

    asked: int = 0  # spikes sent to the labeler (the budget counts these)
    added: Counter[str] = field(default_factory=Counter)  # new labels, per value
    failed: int = 0  # answers that were never valid JSON (asked, not labeled)
    stopped: str = ""  # why it stopped before the end: BUDGET, TIME, LLM_DOWN, WIKI_DOWN
    detail: str = ""  # the error behind LLM_DOWN or WIKI_DOWN


def label_within_budget(
    todo: Sequence[SpikeRecord],
    contexts: Callable[[SpikeRecord], object],
    labeler: Labeler,
    model: str,
    run_id: str,
    path: Path,
    budget: int,
    time_left: Callable[[], bool] = lambda: True,
) -> LabelRun:
    """Label the spikes of `todo` in order, appending each label to `path` at once.

    Stops at the budget (spikes asked), when `time_left()` turns False, or at the first
    sign that the labeler or Wikimedia is down; what was labeled until then is kept. A
    spike some record already covers (a label added since `todo` was made) is skipped,
    so labeling is idempotent even within a run.
    """
    run = LabelRun()
    records = load_records(path)
    for spike in todo:
        if any(record.is_for(spike) for record in records):
            continue
        if run.asked >= budget:
            run.stopped = BUDGET
            break
        if not time_left():
            run.stopped = TIME
            break
        try:
            context = contexts(spike)
        except httpx.HTTPError as error:  # back off: no more requests this run
            run.stopped, run.detail = WIKI_DOWN, f"{type(error).__name__}: {error}"
            break
        try:
            label, reason = labeler.label(context)
        except ValueError as error:  # StructuredOutputError: never a valid answer
            run.asked += 1
            run.failed += 1
            logger.warning("No valid answer for %s: %s", spike.title, error)
            continue
        except LLMError as error:  # not running, model missing, refused: no call made
            run.stopped, run.detail = LLM_DOWN, f"{type(error).__name__}: {error}"
            break
        run.asked += 1
        record = record_for(
            spike, run_id, label, "llm", datetime.now(UTC), model=model, reason=reason
        )
        append_record(path, record)
        records.append(record)
        run.added[label] += 1
        logger.info("[%d/%d] %s: %s (%s)", run.asked, budget, spike.title, label, reason)
    return run


@dataclass
class LabelReport:
    """The task's result, for the log."""

    spikes: int  # spikes that can become rows
    judged_before: int  # already labeled or linked from the portal
    to_label: int  # needing a label at the start
    run: LabelRun
    unlabeled: int  # still needing a label at the end
    unlabeled_testable: int  # of those, on days that could be test days

    def lines(self, budget: int, model: str) -> list[str]:
        added = sum(self.run.added.values())
        values = ", ".join(f"{k} {v}" for k, v in sorted(self.run.added.items())) or "none"
        lines = [
            f"Spikes that can become rows: {self.spikes}; already judged (label or portal "
            f"link): {self.judged_before}; needing a label: {self.to_label}",
            f"Labels added: {added} ({values}) with {model}; budget used: {self.run.asked} "
            f"of {budget} spikes ({self.run.failed} without a valid answer)",
            f"Still unlabeled: {self.unlabeled} spikes, {self.unlabeled_testable} of them "
            "on days that could be test days now",
        ]
        if self.run.stopped:
            why = {
                BUDGET: "the budget ran out (ML_RETRAIN_LABEL_BUDGET)",
                TIME: "the time ran out (ML_RETRAIN_LABEL_MAX_MINUTES)",
                LLM_DOWN: f"the labeler is unavailable ({self.run.detail})",
                WIKI_DOWN: f"Wikimedia's API failed ({self.run.detail})",
            }[self.run.stopped]
            lines.append(
                f"Stopped early: {why}. The build goes on with the labels there are; days "
                "without enough labels wait for a later run."
            )
        return lines


def plan_and_label(
    spikes: Sequence[SpikeRecord],
    portal: Sequence[PortalDay],
    as_of: datetime,
    labels_path: Path,
    labeling: Callable[[list[SpikeRecord]], LabelRun],
    delay_days: int,
) -> LabelReport:
    """Choose the spikes to label (`needs_label`, `label_order`), label them with
    `labeling` (a `label_within_budget` call), and count what is left."""
    from wikipulse.eval.label_store import load_labels

    records = load_records(labels_path)
    todo = [s for s in spikes if needs_label(s, records, portal, as_of)]
    labels = load_labels(labels_path)
    readiness = day_readiness(spikes, portal, labels, as_of, delay_days, min_share=0.0)
    testable = {day for day, r in readiness.items() if r.ready}
    run = labeling(label_order(todo, testable)) if todo else LabelRun()
    records = load_records(labels_path)
    left = [s for s in todo if needs_label(s, records, portal, as_of)]
    return LabelReport(
        spikes=len(spikes),
        judged_before=len(spikes) - len(todo),
        to_label=len(todo),
        run=run,
        unlabeled=len(left),
        unlabeled_testable=sum(day_of(s) in testable for s in left),
    )


def deadline(minutes: float) -> Callable[[], bool]:
    """A `time_left` function: True until `minutes` of wall-clock time have passed.
    (A cost limit for the run, not processing logic: it never changes a label.)"""
    end = time.monotonic() + 60 * minutes
    return lambda: time.monotonic() < end

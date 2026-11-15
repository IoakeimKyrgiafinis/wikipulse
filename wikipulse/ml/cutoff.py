"""A spike's features at its cutoff: the one code path training and serving share.

Spec 11a fixed a training/serving skew: the dataset and the scorer measured features
for different spikes at different moments. To keep it fixed, both now go through the
same functions:

0. `page_edit()`: an archived or streamed edit as the features see it. The dataset reads
   edits from the lake (`dataset.page_edits`), the live scorer from the Redis stream
   (`serve.EditBuffer`); both convert them here.
1. `spike_window()` and `at_cutoff()`: from the spike's start (`first_edit_at`,
   `started_at`) and the page's known edits, the `SpikeWindow` (cutoff =
   `started_at + FEATURE_WINDOW_MIN`) and the page's revision at the cutoff. The caller
   then fetches the snapshots of those revisions in one batch with
   `wikipulse.ml.pages.WikiPages` (MediaWiki API, cached), the same class on both sides.
2. `features_at_cutoff()`: the features (`features.compute_features`), proven free of
   leakage (`check_no_leakage`), with the page as it was at the cutoff.

`wikipulse.ml.dataset.build` and `wikipulse.ml.serve` (`LiveScorer`, `backfill`) both
call them; `tests/ml/test_cutoff.py` checks that a dataset row and a served row of the
same spike have identical features, from the lake and the stream respectively.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from wikipulse.ml.features import PageEdit, PageSnapshot, SpikeWindow, check_no_leakage
from wikipulse.stream.models import EditEvent


def page_edit(event: EditEvent) -> PageEdit:
    """The part of an edit the features use, from the stream or from the lake."""
    return PageEdit(
        time=event.event_time,
        user=event.user,
        is_bot=event.is_bot,
        is_minor=event.is_minor,
        is_new=event.type == "new",
        len_old=event.len_old,
        len_new=event.len_new,
        rev_new=event.rev_new,
        comment=event.comment,
    )


def revision_at(edits: Sequence[PageEdit], cutoff: datetime) -> int | None:
    """The page's revision current at the cutoff: the newest one saved at or before it.

    Two edits in the same second are told apart by their revision id (ids only grow),
    so the answer doesn't depend on the order the edits arrived in.
    """
    known = [e for e in edits if e.time <= cutoff and e.rev_new is not None]
    return max(known, key=lambda e: (e.time, e.rev_new)).rev_new if known else None


def spike_window(
    title: str, first_edit_at: datetime, started_at: datetime, window_min: float
) -> SpikeWindow:
    """The only part of a spike the features may see: its start, and so its cutoff."""
    return SpikeWindow(title, first_edit_at, started_at, window_min)


@dataclass(frozen=True)
class AtCutoff:
    """One spike, ready for its features: its window, the page's edits, its revision."""

    window: SpikeWindow
    edits: Sequence[PageEdit]
    revision: int | None  # the page's revision at the cutoff (None: no known revision)


def at_cutoff(
    title: str,
    first_edit_at: datetime,
    started_at: datetime,
    edits: Sequence[PageEdit],
    window_min: float,
) -> AtCutoff:
    """The spike's feature window and the page's revision at its cutoff.

    Takes the start times, never the stored spike row: its totals describe the spike's
    whole life, which is unknown at the cutoff.
    """
    window = spike_window(title, first_edit_at, started_at, window_min)
    return AtCutoff(window, edits, revision_at(edits, window.cutoff))


def features_at_cutoff(
    item: AtCutoff, snapshots: Mapping[int, PageSnapshot | None]
) -> tuple[dict[str, float], PageSnapshot | None]:
    """(the leakage-checked features, the page snapshot used), from fetched snapshots."""
    page = snapshots.get(item.revision) if item.revision is not None else None
    return check_no_leakage(item.window, item.edits, page), page

"""A spike's features at its cutoff: the one code path training and serving share.

Spec 11a fixed a training/serving skew: the dataset and the scorer measured features
for different spikes at different moments. To keep it fixed, both now go through the
same two functions:

1. `at_cutoff()`: from the spike's start (`first_edit_at`, `started_at`) and the page's
   known edits, the `SpikeWindow` (cutoff = `started_at + FEATURE_WINDOW_MIN`) and the
   page's revision at the cutoff. The caller then fetches the snapshots of those
   revisions in one batch (MediaWiki API, cached).
2. `features_at_cutoff()`: the features (`features.compute_features`), proven free of
   leakage (`check_no_leakage`), with the page as it was at the cutoff.

`wikipulse.ml.dataset.build_row` and `wikipulse.ml.serve.score_spikes` both call them;
`tests/ml/test_cutoff.py` checks that a dataset row and a served row of the same spike
and the same edits have identical features.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from wikipulse.ml.features import PageEdit, PageSnapshot, SpikeWindow, check_no_leakage


def revision_at(edits: Sequence[PageEdit], cutoff: datetime) -> int | None:
    """The page's revision current at the cutoff: the newest one saved at or before it."""
    known = [e for e in edits if e.time <= cutoff and e.rev_new is not None]
    return max(known, key=lambda e: e.time).rev_new if known else None


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
    window = SpikeWindow(title, first_edit_at, started_at, window_min)
    return AtCutoff(window, edits, revision_at(edits, window.cutoff))


def features_at_cutoff(
    item: AtCutoff, snapshots: Mapping[int, PageSnapshot | None]
) -> tuple[dict[str, float], PageSnapshot | None]:
    """(the leakage-checked features, the page snapshot used), from fetched snapshots."""
    page = snapshots.get(item.revision) if item.revision is not None else None
    return check_no_leakage(item.window, item.edits, page), page

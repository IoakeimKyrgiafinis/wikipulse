"""Features of a spike, computed only from what is known `FEATURE_WINDOW_MIN` after it starts.

**The prediction moment.** A spike starts at `started_at` (when a start rule held). The
classifier scores it once, `FEATURE_WINDOW_MIN` (10) minutes later: at the *cutoff*
`started_at + 10 min`. Everything a feature uses must have been observable at the cutoff:

- the page's edits up to the cutoff (the burst that triggered the spike, from
  `first_edit_at`, plus the first 10 minutes after the start), and the page's edits in the
  24 hours before the burst;
- the page as it looked at the cutoff: the last revision saved at or before the cutoff
  (`PageSnapshot`), never the page as it is today;
- the title and the time of day.

**Data leakage** is a feature that quietly uses the future: the spike's total edits, how
long it lasted, its final score, the categories the page has *now* ("2026 deaths" is
often added hours after a death). A model trained on such features looks great in tests
and fails live, where the future is unknown. Two guards make this structural:

1. `compute_features` receives a `SpikeWindow` (title, `first_edit_at`, `started_at`) and
   never the stored spike row with its totals; its first step drops every edit after the
   cutoff (`visible_edits`), and it refuses a page snapshot saved after the cutoff.
2. `check_no_leakage` recomputes the features with all later edits removed, and again
   with invented later edits added; if any value changes, it raises `LeakageError`. The
   dataset builder runs it on every row, and a unit test runs it on a synthetic spike.
"""

import math
import re
import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from wikipulse.detect.detector import is_revert

FEATURE_WINDOW_MIN = 10.0  # default of the FEATURE_WINDOW_MIN setting
HISTORY = timedelta(hours=24)  # how far back before the burst history features look
PEAK_WINDOW = timedelta(minutes=10)

# Anonymous editors: IP addresses (old style) and temporary accounts ("~2026-12345-67").
_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_IPV6 = re.compile(r"^[0-9A-Fa-f]{0,4}(:[0-9A-Fa-f]{0,4}){2,7}$")
_TEMP_ACCOUNT = re.compile(r"^~\d{4}-\d+-\d+$")

NEWS_WORDS = re.compile(
    r"\b(died|dies|death|dead|killed|elected|election|results?|wins?|won|announced|"
    r"resign\w*|arrested|attack\w*|earthquake|breaking|confirmed|released|appointed|"
    r"sworn|winner|declared|launch\w*|crash\w*|shooting|storm|hurricane|update[sd]?)\b",
    re.IGNORECASE,
)
VANDAL_WORDS = re.compile(
    r"\b(vandal\w*|unsourced|disruptive|spam|test edit|nonsense|blp|hoax|pov|"
    r"unconstructive)\b",
    re.IGNORECASE,
)
YEAR = re.compile(r"\b(19|20)\d\d\b")
LIST_TITLE = re.compile(r"^(lists? of|deaths in|timeline of)\b", re.IGNORECASE)

# Every feature, in column order, with what it means. The dataset manifest stores this.
FEATURES: dict[str, str] = {
    # Burst shape: human edits from first_edit_at to the cutoff.
    "edits": "human edits in the burst so far (first_edit_at to cutoff)",
    "editors": "distinct human editors in the burst so far",
    "edits_before_start": "human edits from first_edit_at to started_at (the trigger)",
    "edits_after_start": "human edits in the feature window after started_at",
    "editors_after_start": "distinct human editors after started_at",
    "edits_per_editor": "edits / editors",
    "top_editor_share": "share of the burst's edits made by its busiest editor",
    "anon_share": "share of burst edits by IPs or temporary accounts",
    "revert_share": "share of burst edits that are reverts (by edit summary)",
    "minor_share": "share of burst edits marked minor",
    "bot_edits": "bot edits in the burst so far",
    "new_page": "1 if the page was created in the 24 h before the burst or during it",
    "burst_minutes": "minutes from first_edit_at to the cutoff",
    "detect_delay_min": "minutes from first_edit_at to started_at",
    "edits_per_hour": "burst edits per hour",
    "peak_10m": "most burst edits in any 10 minutes",
    "last_10m_share": "share of burst edits in the last 10 minutes before the cutoff",
    "median_gap_min": "median minutes between consecutive burst edits",
    "bytes_added": "bytes added by burst edits (sum of growth)",
    "bytes_removed": "bytes removed by burst edits (sum of shrinkage, positive)",
    "page_len": "page length in bytes after the last edit before the cutoff",
    "comment_empty_share": "share of burst edits without an edit summary",
    "news_word_share": "share of burst edit summaries with news words (died, elected, ...)",
    "vandal_word_share": "share of burst edit summaries mentioning vandalism or similar",
    # History: the 24 hours before first_edit_at.
    "prior_edits_24h": "human edits in the 24 h before the burst",
    "prior_editors_24h": "distinct human editors in the 24 h before the burst",
    "prior_edits_1h": "human edits in the hour before the burst",
    "hours_since_prior_edit": "hours since the last human edit before the burst (max 24)",
    "rate_ratio": "burst edits per hour / (prior 24 h edits + 1) per hour",
    # Time and title.
    "hour_utc": "UTC hour of started_at",
    "title_words": "words in the title",
    "title_has_year": "1 if the title contains a year (1900-2099)",
    "title_has_spike_year": "1 if the title contains the year of the spike",
    "title_is_list": "1 for 'List of ...', 'Deaths in ...', 'Timeline of ...'",
    "title_has_parenthesis": "1 for a disambiguated title such as 'X (film)'",
    # The page as it was at the cutoff (last revision at or before it; NaN if unknown).
    "page_age_days": "days from the page's creation to the cutoff",
    "categories": "explicit categories in the wikitext",
    "cat_living_people": "1 if in 'Living people'",
    "cat_deaths_spike_year": "1 if in '<spike year> deaths'",
    "cat_mentions_spike_year": "1 if any category names the spike's year",
    "is_biography": "1 if in a births, deaths or 'Living people' category",
    "has_current_template": "1 if the page carries {{Current}}, {{Recent death}}, ...",
    "refs": "number of <ref> tags in the wikitext",
}
FEATURE_NAMES: list[str] = list(FEATURES)


class LeakageError(RuntimeError):
    """A feature used information from after the cutoff."""


@dataclass(frozen=True)
class PageEdit:
    """One edit of a page, as much as the features need (from the lake)."""

    time: datetime
    user: str
    is_bot: bool = False
    is_minor: bool = False
    is_new: bool = False  # the edit created the page
    len_old: int | None = None
    len_new: int | None = None
    rev_new: int | None = None
    comment: str = ""


@dataclass(frozen=True)
class SpikeWindow:
    """What the features may know about a spike: when it began and when it started.

    Deliberately *not* the stored spike row: its totals, end and score describe the
    whole life of the spike, which is unknown at prediction time.
    """

    title: str
    first_edit_at: datetime
    started_at: datetime
    window_min: float = FEATURE_WINDOW_MIN

    @property
    def cutoff(self) -> datetime:
        """The prediction moment: `window_min` minutes after the spike started."""
        return self.started_at + timedelta(minutes=self.window_min)


@dataclass(frozen=True)
class PageSnapshot:
    """The page at its last revision at or before the cutoff (see `ml/pages.py`)."""

    revision_id: int
    revision_time: datetime
    categories: tuple[str, ...] = ()
    templates: tuple[str, ...] = ()  # lower-case template names used in the wikitext
    refs: int = 0
    created_at: datetime | None = None  # the page's first revision


def visible_edits(edits: Sequence[PageEdit], cutoff: datetime) -> list[PageEdit]:
    """The edits known at the cutoff, oldest first. The only door edits come through."""
    return sorted((e for e in edits if e.time <= cutoff), key=lambda e: e.time)


def usable_snapshot(page: PageSnapshot | None, cutoff: datetime) -> PageSnapshot | None:
    """The snapshot, if it was taken at or before the cutoff.

    A snapshot of a later revision is a bug in the caller (it would leak the page's
    future), so it raises. An unknown or impossible creation time (after the cutoff, e.g.
    a page deleted and created again later) is dropped.
    """
    if page is None:
        return None
    if page.revision_time > cutoff:
        raise LeakageError(
            f"page snapshot of revision {page.revision_id} ({page.revision_time}) "
            f"is after the cutoff {cutoff}"
        )
    if page.created_at is not None and page.created_at > cutoff:
        page = replace(page, created_at=None)
    return page


def compute_features(
    window: SpikeWindow, edits: Sequence[PageEdit], page: PageSnapshot | None = None
) -> dict[str, float]:
    """All features of one spike, in `FEATURE_NAMES` order. Unknown values are NaN."""
    visible = visible_edits(edits, window.cutoff)
    snapshot = usable_snapshot(page, window.cutoff)
    features = {
        **burst_features(window, visible),
        **history_features(window, visible),
        **title_features(window),
        **page_features(window, snapshot),
    }
    if list(features) != FEATURE_NAMES:
        raise RuntimeError("feature functions and FEATURES disagree")
    return features


def check_no_leakage(
    window: SpikeWindow, edits: Sequence[PageEdit], page: PageSnapshot | None = None
) -> dict[str, float]:
    """Compute the features and prove they ignore everything after the cutoff.

    Recomputes them (1) from only the edits up to the cutoff and (2) with a burst of
    invented edits after it (new editors, reverts, bots, a new page length). If any
    value differs, a feature depends on the future: `LeakageError`. Returns the features.
    """
    features = compute_features(window, edits, page)
    cutoff = window.cutoff
    past_only = [e for e in edits if e.time <= cutoff]
    future = [
        PageEdit(
            time=cutoff + timedelta(seconds=1 + 30 * i),
            user=f"~2099-{i}-1" if i % 2 else f"Future editor {i}",
            is_bot=i % 5 == 0,
            is_minor=i % 3 == 0,
            len_old=10_000 + i,
            len_new=20_000 + i,
            rev_new=10**12 + i,
            comment="Reverted edits by vandal; died, elected" if i % 2 else "",
        )
        for i in range(40)
    ]
    for variant in (past_only, [*edits, *future]):
        if not same_values(features, compute_features(window, variant, page)):
            raise LeakageError(f"features of {window.title!r} change with edits after the cutoff")
    return features


def same_values(a: dict[str, float], b: dict[str, float]) -> bool:
    """Equal dicts, counting NaN as equal to NaN."""

    def same(x: float, y: float) -> bool:
        return (math.isnan(x) and math.isnan(y)) or x == y

    return a.keys() == b.keys() and all(same(a[key], b[key]) for key in a)


# --- the groups of features -----------------------------------------------------------


def is_anonymous(user: str) -> bool:
    """An IP address or a temporary account (how logged-out editors appear since 2025)."""
    return bool(_TEMP_ACCOUNT.match(user) or _IPV4.match(user) or _IPV6.match(user))


def share(part: int, whole: int) -> float:
    """`part / whole`, or NaN when `whole` is 0."""
    return part / whole if whole else math.nan


def peak_count(times: Sequence[datetime], width: timedelta) -> int:
    """Most items in any stretch of `width` (times sorted): a two-pointer pass."""
    best, start = 0, 0
    for end, moment in enumerate(times):
        while moment - times[start] >= width:
            start += 1
        best = max(best, end - start + 1)
    return best


def size_change(edit: PageEdit) -> int:
    """Bytes an edit added (negative: removed)."""
    if edit.len_new is None:
        return 0
    return edit.len_new - (edit.len_old or 0)


def burst_features(window: SpikeWindow, visible: Sequence[PageEdit]) -> dict[str, float]:
    """Features of the burst up to the cutoff: edits, editors, shares, timing."""
    start, cutoff = window.first_edit_at, window.cutoff
    in_burst = [e for e in visible if e.time >= start]
    burst = [e for e in in_burst if not e.is_bot]
    after = [e for e in burst if e.time > window.started_at]
    users = Counter(e.user for e in burst)
    n = len(burst)
    minutes = (cutoff - start).total_seconds() / 60
    times = [e.time for e in burst]
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(times, times[1:], strict=False)]
    changes = [size_change(e) for e in burst]
    lengths = [e.len_new for e in visible if e.len_new is not None]
    comments = [e.comment for e in burst]
    created = any(e.is_new and e.time >= start - HISTORY for e in visible)
    return {
        "edits": n,
        "editors": len(users),
        "edits_before_start": n - len(after),
        "edits_after_start": len(after),
        "editors_after_start": len({e.user for e in after}),
        "edits_per_editor": share(n, len(users)),
        "top_editor_share": share(max(users.values(), default=0), n),
        "anon_share": share(sum(is_anonymous(e.user) for e in burst), n),
        "revert_share": share(sum(is_revert(c) for c in comments), n),
        "minor_share": share(sum(e.is_minor for e in burst), n),
        "bot_edits": len(in_burst) - n,
        "new_page": float(created),
        "burst_minutes": minutes,
        "detect_delay_min": (window.started_at - start).total_seconds() / 60,
        "edits_per_hour": n / (minutes / 60) if minutes > 0 else math.nan,
        "peak_10m": peak_count(times, PEAK_WINDOW),
        "last_10m_share": share(sum(t > cutoff - PEAK_WINDOW for t in times), n),
        "median_gap_min": statistics.median(gaps) if gaps else math.nan,
        "bytes_added": sum(c for c in changes if c > 0),
        "bytes_removed": -sum(c for c in changes if c < 0),
        "page_len": lengths[-1] if lengths else math.nan,
        "comment_empty_share": share(sum(not c.strip() for c in comments), n),
        "news_word_share": share(sum(bool(NEWS_WORDS.search(c)) for c in comments), n),
        "vandal_word_share": share(sum(bool(VANDAL_WORDS.search(c)) for c in comments), n),
    }


def history_features(window: SpikeWindow, visible: Sequence[PageEdit]) -> dict[str, float]:
    """Features of the page's editing in the 24 hours before the burst."""
    start = window.first_edit_at
    prior = [e for e in visible if start - HISTORY <= e.time < start and not e.is_bot]
    last_hour = [e for e in prior if e.time >= start - timedelta(hours=1)]
    since = (
        (start - prior[-1].time).total_seconds() / 3600 if prior else HISTORY / timedelta(hours=1)
    )
    burst = [e for e in visible if e.time >= start and not e.is_bot]
    hours = (window.cutoff - start).total_seconds() / 3600
    burst_rate = len(burst) / hours if hours > 0 else math.nan
    return {
        "prior_edits_24h": len(prior),
        "prior_editors_24h": len({e.user for e in prior}),
        "prior_edits_1h": len(last_hour),
        "hours_since_prior_edit": since,
        "rate_ratio": burst_rate / ((len(prior) + 1) / 24),
    }


def title_features(window: SpikeWindow) -> dict[str, float]:
    """Features of the title and the start time."""
    title = window.title
    started = window.started_at.astimezone(UTC)
    year = str(started.year)
    return {
        "hour_utc": started.hour,
        "title_words": len(title.split()),
        "title_has_year": float(bool(YEAR.search(title))),
        "title_has_spike_year": float(year in title),
        "title_is_list": float(bool(LIST_TITLE.match(title))),
        "title_has_parenthesis": float("(" in title),
    }


CURRENT_TEMPLATES = ("current", "recent death", "ongoing", "developing")


def page_features(window: SpikeWindow, page: PageSnapshot | None) -> dict[str, float]:
    """Features of the page as it was at the cutoff (NaN if unknown)."""
    if page is None:
        return {name: math.nan for name in PAGE_FEATURES}
    year = str(window.started_at.astimezone(UTC).year)
    cats = [c.lower() for c in page.categories]
    age = (
        (window.cutoff - page.created_at).total_seconds() / 86400
        if page.created_at is not None
        else math.nan
    )
    return {
        "page_age_days": age,
        "categories": len(cats),
        "cat_living_people": float("living people" in cats),
        "cat_deaths_spike_year": float(f"{year} deaths" in cats),
        "cat_mentions_spike_year": float(any(year in c for c in cats)),
        "is_biography": float(
            "living people" in cats or any(re.search(r"\d (births|deaths)$", c) for c in cats)
        ),
        "has_current_template": float(any(t.startswith(CURRENT_TEMPLATES) for t in page.templates)),
        "refs": page.refs,
    }


PAGE_FEATURES = [
    "page_age_days",
    "categories",
    "cat_living_people",
    "cat_deaths_spike_year",
    "cat_mentions_spike_year",
    "is_biography",
    "has_current_template",
    "refs",
]

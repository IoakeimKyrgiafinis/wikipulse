"""Vandalism signals of spike bursts (spec 11c): which ones separate news from not news?

    uv run python -m wikipulse.eval.vandalism              # the full analysis (~8 minutes)
    uv run python -m wikipulse.eval.vandalism --no-rules   # the signals only (seconds)

Spec 02b found that most "not news" spikes were vandalism bursts. Before adding a rule,
this analysis measures which properties of a burst actually tell news from not news:

1. **Labeled bursts.** `evals/labels/spike_labels.jsonl`, grouped into bursts (same
   article, overlapping periods). Each burst gives its human label and its latest LLM
   label *separately*, so every number exists once per labeler (12b found the LLM
   over-calls news). The CI gate's labels (`detection_gate_labels.jsonl`, LLM) are a
   third set. `unsure` labels are left out and counted.
2. **Edits** of those pages, from the history lake (`ML_LAKE_PREFIX`).
3. **Signals** (`SIGNALS`) at three moments: `start`, the window the detector's start
   rule saw (what a detector rule can use); `cutoff`, from the burst's first edit to 10
   minutes after the start (what the news classifier sees); `burst`, the whole burst
   (hindsight, only to describe the bursts). Anonymous editors are counted, never named:
   user names are compared in memory and never leave this module.
4. **Separation**: per signal, the mean among news and not-news bursts and the AUC, the
   chance that a random not-news burst has a higher value than a random news burst (0.5:
   no separation), with a 95% bootstrap interval.
5. **Candidate rules** (`RULES`): the detector over the whole lake once per value of
   `max_anon_share`, scored against human and LLM labels (precision with a Wilson
   interval, labeled news bursts still found), and on the CI gate's fixture.

Writes `evals/results/<date>_vandalism.md` and `.json`. Reads the lake (S3) and the label
files; no network beyond the lake, no database, no LLM.
"""

import argparse
import json
import logging
import math
import random
import re
import statistics
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from wikipulse.config import DetectorConfig, Settings, get_settings
from wikipulse.detect.detector import SpikeRecord, is_anonymous, is_revert
from wikipulse.eval import gate
from wikipulse.eval.audit import wilson
from wikipulse.eval.label_store import DEFAULT_LABELS_PATH, LabelRecord, load_records
from wikipulse.eval.metrics import Label, percentile, pick_label
from wikipulse.lake.store import LakeStore
from wikipulse.logging_setup import setup_logging
from wikipulse.ml.dataset import contiguous_hours, detect_spikes, lake_events, page_edits
from wikipulse.ml.features import FEATURE_WINDOW_MIN, VANDAL_WORDS, PageEdit

logger = logging.getLogger(__name__)

RESULTS_DIR = Path("evals") / "results"
DEFAULT_RESAMPLES = 1000
DEFAULT_SEED = 2026
NOT_NEWS = "not news"
# Values of `max_anon_share` the detector is run with; 1.0 is the rule switched off.
RULES = (1.0, 0.75, 0.6, 0.5, 0.45, 0.4)
MAX_LISTED = 50  # dropped bursts listed per label set in the report (news first)

SIGNALS: dict[str, str] = {
    "revert_share": "reverts (by edit summary, humans and bots) among all the period's edits",
    "reverted_share": "share of the period's human non-revert edits already reverted",
    "revert_minutes": "median minutes from a reverted edit to its revert (bursts with one)",
    "anon_share": "share of the period's human non-revert edits by anonymous editors",
    "anon_editors": "distinct anonymous editors (a count)",
    "registered_editors": "distinct registered editors (a count)",
    "top_editor_share": "share of the period's human non-revert edits by its busiest editor",
    "vandal_word_share": "share of human edit summaries mentioning vandalism or similar",
}
# The news classifier's features (wikipulse/ml/features.py) that already measure a signal
# at the `cutoff` moment.
CLASSIFIER_FEATURES: dict[str, str] = {
    "revert_share": "revert_share",
    "anon_share": "anon_share",
    "top_editor_share": "top_editor_share",
    "vandal_word_share": "vandal_word_share",
}
MOMENTS: dict[str, str] = {
    "start": "the window the start rule saw (10 or 60 minutes up to the start)",
    "cutoff": "first edit to 10 minutes after the start (the classifier's view)",
    "burst": "the whole burst (hindsight)",
}

# Revert summaries that name what they undo: "Undid revision 123 by X", "Reverted 2 edits
# by X". Others ("Reverted edits by X", "rvv") undo the last editor's latest edits.
UNDID = re.compile(r"^\s*undid revision (\d+)", re.IGNORECASE)
REVERTED_COUNT = re.compile(r"\breverted (\d+) edits?\b", re.IGNORECASE)


# --- Labeled bursts -----------------------------------------------------------------


@dataclass(frozen=True)
class LabeledBurst:
    """One burst with one labeler's verdict (the latest of that labeler)."""

    title: str
    first_edit_at: datetime
    started_at: datetime
    end_at: datetime
    label: str  # "news" or "not news"
    labeler: str  # "human" or "llm"
    model: str | None

    @property
    def news(self) -> bool:
        return self.label == "news"

    def to_label(self) -> Label:
        return Label(
            self.title, self.first_edit_at, self.end_at, self.label, self.labeler, self.model
        )


def burst_groups(records: Sequence[LabelRecord]) -> list[list[LabelRecord]]:
    """Records of the same article whose periods overlap (chained), in file order."""
    groups: list[list[LabelRecord]] = []
    end: datetime | None = None
    ordered = sorted(enumerate(records), key=lambda p: (p[1].title, p[1].first_edit_at, p[0]))
    for _, record in ordered:
        last = groups[-1] if groups else None
        if last and last[0].title == record.title and end and record.first_edit_at <= end:
            last.append(record)
            end = max(end, record.end_at)
        else:
            groups.append([record])
            end = record.end_at
    index = {id(r): i for i, r in enumerate(records)}
    return [sorted(group, key=lambda r: index[id(r)]) for group in groups]


def labeled_bursts(records: Sequence[LabelRecord], labeler: str) -> tuple[list[LabeledBurst], int]:
    """One burst per group with a decisive label of `labeler`; and how many were unsure."""
    bursts, unsure = [], 0
    for group in burst_groups(records):
        mine = [r for r in group if r.labeler == labeler]
        if not mine:
            continue
        record = mine[-1]
        if record.label not in ("news", NOT_NEWS):
            unsure += 1
            continue
        bursts.append(
            LabeledBurst(
                record.title,
                record.first_edit_at,
                record.started_at,
                record.end_at,
                record.label,
                labeler,
                record.model,
            )
        )
    return sorted(bursts, key=lambda b: (b.first_edit_at, b.title)), unsure


# --- Signals --------------------------------------------------------------------------


def reverted_at(edits: Sequence[PageEdit]) -> dict[int, datetime]:
    """For each edit (by index) a later revert undid: the time of that revert.

    "Undid revision N" undoes the edit that saved revision N. Other reverts undo the
    latest edits of the last editor before them who isn't the reverter ("Reverted 2
    edits by X": at most 2). Good enough to count; never shown per user.
    """
    found: dict[int, datetime] = {}
    by_revision = {e.rev_new: i for i, e in enumerate(edits) if e.rev_new is not None}
    for i, edit in enumerate(edits):
        if not is_revert(edit.comment):
            continue
        undid = UNDID.match(edit.comment)
        target = by_revision.get(int(undid.group(1))) if undid else None
        if target is not None and target < i:
            found.setdefault(target, edit.time)
            continue
        j = i - 1
        while j >= 0 and edits[j].user == edit.user:
            j -= 1
        count = REVERTED_COUNT.search(edit.comment)
        limit = int(count.group(1)) if count else len(edits)
        undone, last_user = 0, edits[j].user if j >= 0 else None
        while j >= 0 and edits[j].user == last_user and undone < limit:
            found.setdefault(j, edit.time)
            j, undone = j - 1, undone + 1
    return found


def share(part: int, whole: int) -> float:
    return part / whole if whole else math.nan


def period_signals(
    edits: Sequence[PageEdit],
    reverted: dict[int, datetime],
    period: tuple[datetime, datetime, bool],
    known_at: datetime,
) -> dict[str, float]:
    """The signals of the edits in `period` (start, end, start included?), as known at
    `known_at` (a revert after it isn't known yet)."""
    start, end, closed = period
    inside = [
        i
        for i, e in enumerate(edits)
        if (e.time >= start if closed else e.time > start) and e.time <= end
    ]
    reverts = [i for i in inside if is_revert(edits[i].comment)]
    human = [i for i in inside if not edits[i].is_bot]
    content = [i for i in human if not is_revert(edits[i].comment)]
    undone = [i for i in content if i in reverted and reverted[i] <= known_at]
    users = [edits[i].user for i in content]
    anonymous = {u for u in users if is_anonymous(u)}
    lags = [(reverted[i] - edits[i].time).total_seconds() / 60 for i in undone]
    counts = [users.count(u) for u in set(users)]
    return {
        "revert_share": share(len(reverts), len(inside)),
        "reverted_share": share(len(undone), len(content)),
        "revert_minutes": statistics.median(lags) if lags else math.nan,
        "anon_share": share(sum(is_anonymous(u) for u in users), len(users)),
        "anon_editors": len(anonymous),
        "registered_editors": len(set(users) - anonymous),
        "top_editor_share": share(max(counts, default=0), len(users)),
        "vandal_word_share": share(
            sum(bool(VANDAL_WORDS.search(edits[i].comment)) for i in human), len(human)
        ),
    }


def moments_of(
    burst: LabeledBurst, config: DetectorConfig
) -> dict[str, tuple[tuple[datetime, datetime, bool], datetime]]:
    """Each moment's period and when it is judged."""
    short = timedelta(minutes=config.short_window_min)
    long = timedelta(minutes=config.long_window_min)
    window = short if burst.first_edit_at >= burst.started_at - short else long
    cutoff = burst.started_at + timedelta(minutes=FEATURE_WINDOW_MIN)
    return {
        "start": ((burst.started_at - window, burst.started_at, False), burst.started_at),
        "cutoff": ((burst.first_edit_at, cutoff, True), cutoff),
        "burst": ((burst.first_edit_at, burst.end_at, True), burst.end_at + timedelta(hours=1)),
    }


def burst_signals(
    burst: LabeledBurst, edits: Sequence[PageEdit], config: DetectorConfig
) -> dict[str, dict[str, float]] | None:
    """The signals of one burst at every moment; None if the lake has no edit in it."""
    ordered = sorted(edits, key=lambda e: (e.time, e.rev_new or 0))
    reverted = reverted_at(ordered)
    result = {
        name: period_signals(ordered, reverted, period, known)
        for name, (period, known) in moments_of(burst, config).items()
    }
    if math.isnan(result["burst"]["anon_share"]):
        return None
    return result


# --- Separation -----------------------------------------------------------------------


def auc(news: Sequence[float], not_news: Sequence[float]) -> float | None:
    """P(a not-news value > a news value), ties counting half; NaN values left out.

    Computed from ranks (the Mann-Whitney U statistic), so it is fast for large sets.
    """
    pos = [v for v in not_news if not math.isnan(v)]
    neg = [v for v in news if not math.isnan(v)]
    if not pos or not neg:
        return None
    values = sorted([(v, 1) for v in pos] + [(v, 0) for v in neg])
    rank_sum, i = 0.0, 0
    while i < len(values):
        j = i
        while j < len(values) and values[j][0] == values[i][0]:
            j += 1
        average = (i + 1 + j) / 2  # ranks i+1 .. j share their average
        rank_sum += average * sum(flag for _, flag in values[i:j])
        i = j
    u = rank_sum - len(pos) * (len(pos) + 1) / 2
    return u / (len(pos) * len(neg))


def auc_interval(
    rows: Sequence[tuple[bool, float]], resamples: int, seed: int
) -> tuple[float | None, float | None]:
    """95% bootstrap percentile interval of the AUC over (news?, value) rows."""
    rng = random.Random(seed)
    values = []
    for _ in range(resamples):
        sample = [rows[rng.randrange(len(rows))] for _ in rows]
        value = auc([v for n, v in sample if n], [v for n, v in sample if not n])
        if value is not None:
            values.append(value)
    if not values:
        return None, None
    return percentile(values, 2.5), percentile(values, 97.5)


def verdict(low: float | None, high: float | None) -> str:
    """Words for an AUC interval: does the signal separate the classes?"""
    if low is None or high is None:
        return "too few values"
    if low > 0.5:
        return "higher in not news"
    if high < 0.5:
        return "higher in news"
    return "no clear separation"


def mean_of(values: Iterable[float]) -> float | None:
    known = [v for v in values if not math.isnan(v)]
    return statistics.fmean(known) if known else None


def separation(
    rows: Sequence[tuple[LabeledBurst, dict[str, dict[str, float]]]],
    resamples: int,
    seed: int,
) -> list[dict[str, Any]]:
    """Per signal and moment: counts, class means, AUC and its interval."""
    table = []
    for signal in SIGNALS:
        for moment in MOMENTS:
            pairs = [(b.news, s[moment][signal]) for b, s in rows]
            known = [(n, v) for n, v in pairs if not math.isnan(v)]
            low, high = auc_interval(known, resamples, seed) if known else (None, None)
            table.append(
                {
                    "signal": signal,
                    "moment": moment,
                    "n_news": sum(n for n, _ in known),
                    "n_not_news": sum(not n for n, _ in known),
                    "mean_news": mean_of(v for n, v in known if n),
                    "mean_not_news": mean_of(v for n, v in known if not n),
                    "auc": auc([v for n, v in known if n], [v for n, v in known if not n]),
                    "auc_low": low,
                    "auc_high": high,
                    "verdict": verdict(low, high),
                }
            )
    return table


# --- Candidate rules ------------------------------------------------------------------


def covered(burst: LabeledBurst, spikes: Sequence[SpikeRecord]) -> bool:
    label = burst.to_label()
    return any(label.covers(s) for s in spikes)


def label_counts(spikes: Sequence[SpikeRecord], bursts: Sequence[LabeledBurst]) -> dict[str, Any]:
    """Precision of a run's spikes on one label set, with a Wilson interval."""
    labels = [b.to_label() for b in bursts]
    counts = {"news": 0, NOT_NEWS: 0, "unlabeled": 0}
    for spike in spikes:
        chosen = pick_label([lb for lb in labels if lb.covers(spike)])
        counts[chosen.label if chosen else "unlabeled"] += 1
    n = counts["news"] + counts[NOT_NEWS]
    interval = wilson(counts["news"], n)
    return {
        **counts,
        "n": n,
        "precision": share(counts["news"], n) if n else None,
        "interval": list(interval) if interval else None,
    }


def overlaps(a: LabeledBurst, b: LabeledBurst) -> bool:
    """Same article, overlapping periods: the same burst."""
    return a.title == b.title and a.first_edit_at <= b.end_at and b.first_edit_at <= a.end_at


def dropped_item(burst: LabeledBurst, others: dict[str, list[LabeledBurst]]) -> dict[str, Any]:
    """A burst a rule drops, with the labels the other sets give the same burst."""
    other_labels = {
        name: next(b.label for b in bursts if overlaps(burst, b))
        for name, bursts in others.items()
        if any(overlaps(burst, b) for b in bursts)
    }
    return {
        "title": burst.title,
        "first_edit_at": burst.first_edit_at,
        "label": burst.label,
        "other_labels": other_labels,
    }


def rule_results(
    runs: dict[float, list[SpikeRecord]],
    sets: dict[str, list[LabeledBurst]],
    scored_from: datetime,
) -> list[dict[str, Any]]:
    """Per rule value: spikes, precision per label set, labeled bursts still found."""
    baseline = runs[1.0]
    results = []
    for value, spikes in runs.items():
        scored = [s for s in spikes if s.first_edit_at >= scored_from]
        row: dict[str, Any] = {"max_anon_share": value, "spikes": len(scored), "sets": {}}
        for name, bursts in sets.items():
            found_before = [b for b in bursts if covered(b, baseline)]
            found = [b for b in found_before if covered(b, spikes)]
            row["sets"][name] = {
                **label_counts(scored, bursts),
                "news_found_before": sum(b.news for b in found_before),
                "news_found": sum(b.news for b in found),
                "not_news_found_before": sum(not b.news for b in found_before),
                "not_news_found": sum(not b.news for b in found),
                "dropped": [
                    dropped_item(b, {k: v for k, v in sets.items() if k != name})
                    for b in found_before
                    if b not in found
                ],
            }
        results.append(row)
    return results


def gate_results(fixture: Path, labels: Path) -> list[dict[str, Any]]:
    """The CI gate's numbers for each rule value (the other settings at their defaults)."""
    info, events = gate.read_fixture(fixture)
    gate_labels = [r.to_label() for r in load_records(labels)]
    rows = []
    for value in RULES:
        config = DetectorConfig(max_anon_share=value)
        result = gate.score(gate.detect(events, config), gate_labels, info.evaluate_from, info.end)
        rows.append(
            {
                "max_anon_share": value,
                "spikes": result.spikes,
                "counts": result.counts,
                "precision": result.precision,
                "recall": result.recall,
                "found": result.found,
                "news_bursts": result.news_bursts,
                "found_bursts": result.found_bursts,
            }
        )
    return rows


# --- The analysis ---------------------------------------------------------------------


@dataclass
class Inputs:
    """What the analysis reads."""

    labels: Path
    gate_labels: Path
    fixture: Path
    resamples: int
    seed: int
    rules: bool


def label_sets(inputs: Inputs) -> tuple[dict[str, list[LabeledBurst]], dict[str, Any]]:
    """The three label sets and their description (labelers, models, unsure counts)."""
    spike_records = load_records(inputs.labels)
    gate_records = load_records(inputs.gate_labels)
    sets, info = {}, {}
    for name, records, labeler in (
        ("human", spike_records, "human"),
        ("llm", spike_records, "llm"),
        ("gate (llm)", gate_records, "llm"),
    ):
        bursts, unsure = labeled_bursts(records, labeler)
        sets[name] = bursts
        models = sorted({b.model for b in bursts if b.model})
        info[name] = {
            "file": str(inputs.gate_labels if name.startswith("gate") else inputs.labels),
            "labeler": labeler,
            "models": models,
            "news": sum(b.news for b in bursts),
            "not_news": sum(not b.news for b in bursts),
            "unsure_left_out": unsure,
        }
    return sets, info


def analyse(inputs: Inputs, settings: Settings) -> dict[str, Any]:
    """The whole analysis as one JSON-ready dict."""
    config = DetectorConfig()
    sets, set_info = label_sets(inputs)
    store = LakeStore.from_settings(settings, settings.ml_lake_prefix)
    hours, _ = contiguous_hours(store.hours())
    titles = {b.title for bursts in sets.values() for b in bursts}
    edits = page_edits(store, titles)
    logger.info("Lake: %d hours, %d labeled pages", len(hours), len(titles))
    signals: dict[str, list[dict[str, Any]]] = {}
    for name, bursts in sets.items():
        rows = []
        for burst in bursts:
            values = burst_signals(burst, edits.get(burst.title, []), config)
            if values is not None:
                rows.append((burst, values))
        set_info[name]["with_edits"] = len(rows)
        set_info[name]["with_edits_news"] = sum(b.news for b, _ in rows)
        signals[name] = separation(rows, inputs.resamples, inputs.seed)
        logger.info("Signals for %s: %d bursts with edits in the lake", name, len(rows))
    report: dict[str, Any] = {
        "spec": "11c",
        "lake": {"prefix": settings.ml_lake_prefix, "from": hours[0], "to": hours[-1]},
        "sets": set_info,
        "signals": signals,
        "current_max_anon_share": settings.detector_config().max_anon_share,
    }
    if inputs.rules:
        scored_from = hours[0] + timedelta(hours=settings.ml_warmup_hours)
        configs = {str(v): DetectorConfig(max_anon_share=v) for v in RULES}
        runs = detect_spikes(lake_events(store, hours), configs)
        by_value = {float(k): v for k, v in runs.items()}
        lake_sets = {
            name: [b for b in bursts if b.first_edit_at >= scored_from]
            for name, bursts in sets.items()
            if not name.startswith("gate")
        }
        report["rules"] = {
            "scored_from": scored_from,
            "lake": rule_results(by_value, lake_sets, scored_from),
            "gate": gate_results(inputs.fixture, inputs.gate_labels),
        }
    return report


# --- The report -----------------------------------------------------------------------


def num(value: float | None, digits: int = 2) -> str:
    return (
        "n/a"
        if value is None or (isinstance(value, float) and math.isnan(value))
        else (f"{value:.{digits}f}")
    )


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.0f}%"


def interval_text(values: Sequence[float] | None) -> str:
    return "" if not values else f" [{pct(values[0])}, {pct(values[1])}]"


def render_sets(report: dict[str, Any]) -> Iterable[str]:
    yield (
        "| Label set | File | Labeler (models) | News | Not news | Unsure, left out | "
        "With edits in the lake (news) |"
    )
    yield "|---|---|---|---:|---:|---:|---:|"
    for name, s in report["sets"].items():
        models = ", ".join(s["models"]) or "-"
        yield (
            f"| {name} | `{Path(s['file']).name}` | {s['labeler']} ({models}) | {s['news']} | "
            f"{s['not_news']} | {s['unsure_left_out']} | {s.get('with_edits', '-')} "
            f"({s.get('with_edits_news', '-')}) |"
        )


def render_signals(name: str, table: Sequence[dict[str, Any]]) -> Iterable[str]:
    yield f"### {name}"
    yield ""
    yield "| Signal | Moment | n (news / not) | Mean news | Mean not news | AUC [95%] | Reading |"
    yield "|---|---|---|---:|---:|---|---|"
    for row in table:
        yield (
            f"| {row['signal']} | {row['moment']} | {row['n_news']} / {row['n_not_news']} | "
            f"{num(row['mean_news'])} | {num(row['mean_not_news'])} | {num(row['auc'])} "
            f"[{num(row['auc_low'])}, {num(row['auc_high'])}] | {row['verdict']} |"
        )
    yield ""


def render_rules(rules: dict[str, Any], current: float) -> Iterable[str]:
    lake = rules["lake"]
    names = list(lake[0]["sets"])
    yield f"Spikes scored: first edit from {rules['scored_from']:%Y-%m-%d %H:%M} UTC on."
    yield ""
    header = "| `max_anon_share` | Spikes |"
    rule = "|---|---:|"
    for name in names:
        header += f" Precision, {name} (n) | News bursts found, {name} | Not news found, {name} |"
        rule += "---|---:|---:|"
    yield header
    yield rule
    for row in lake:
        mark = " (chosen)" if row["max_anon_share"] == current else ""
        mark = " (off)" if row["max_anon_share"] == 1.0 else mark
        line = f"| {row['max_anon_share']}{mark} | {row['spikes']} |"
        for name in names:
            s = row["sets"][name]
            line += (
                f" {pct(s['precision'])}{interval_text(s['interval'])} ({s['n']}) |"
                f" {s['news_found']} of {s['news_found_before']} |"
                f" {s['not_news_found']} of {s['not_news_found_before']} |"
            )
        yield line
    yield ""
    yield "On the CI gate's fixture (labels: LLM, see the set `gate (llm)`):"
    yield ""
    yield "| `max_anon_share` | Spikes scored | Precision | Recall (news bursts found) |"
    yield "|---|---:|---:|---|"
    for row in rules["gate"]:
        yield (
            f"| {row['max_anon_share']} | {row['spikes']} | {num(row['precision'], 3)} | "
            f"{num(row['recall'], 3)} ({row['found']} of {row['news_bursts']}) |"
        )
    yield ""


def render_dropped(rules: dict[str, Any], current: float) -> Iterable[str]:
    chosen = next((r for r in rules["lake"] if r["max_anon_share"] == current), None)
    if chosen is None or current == 1.0:
        return
    yield f"### Labeled bursts the rule drops (`max_anon_share` = {current})"
    yield ""
    for name, s in chosen["sets"].items():
        dropped = sorted(s["dropped"], key=lambda d: (d["label"] != "news", d["first_edit_at"]))
        news = [d for d in dropped if d["label"] == "news"]
        yield f"{name}: {len(dropped)} bursts, {len(news)} of them labeled news."
        for other in sorted({k for d in news for k in d["other_labels"]}):
            seen = [d["other_labels"][other] for d in news if other in d["other_labels"]]
            yield (
                f"Of those {len(news)} news bursts, {len(seen)} also have a `{other}` label: "
                f"{seen.count('news')} news, {seen.count(NOT_NEWS)} not news."
            )
        yield ""
        for item in dropped[:MAX_LISTED]:
            moment = item["first_edit_at"]
            others = "".join(f"; {k}: {v}" for k, v in sorted(item["other_labels"].items()))
            yield f"- {item['title']} ({moment:%Y-%m-%d %H:%M} UTC): {item['label']}{others}"
        if len(dropped) > MAX_LISTED:
            yield f"- ... and {len(dropped) - MAX_LISTED} more (in the JSON report)"
        yield ""


def render_markdown(report: dict[str, Any]) -> str:
    lake = report["lake"]
    lines = [
        "# Vandalism signals (spec 11c)",
        "",
        f"Lake `{lake['prefix']}`, {lake['from']:%Y-%m-%d %H:%M} to {lake['to']:%Y-%m-%d %H:%M}"
        " UTC (hour partitions). Made by `python -m wikipulse.eval.vandalism`.",
        "",
        "## Labels",
        "",
        *render_sets(report),
        "",
        "A burst is one article's overlapping labeled spikes; each set uses that labeler's "
        "latest verdict. Human and LLM numbers are kept apart (spec 12b: the LLM over-calls "
        "news).",
        "",
        "## Signals",
        "",
        *(f"- `{name}`: {text}" for name, text in SIGNALS.items()),
        "",
        "Moments: " + "; ".join(f"`{name}` = {text}" for name, text in MOMENTS.items()) + ".",
        "",
        "AUC: the chance that a random not-news burst has a higher value than a random news "
        "burst (0.5 = no separation, 1 = perfect). Intervals: 95% bootstrap over bursts. "
        "Counts are of editors, never names.",
        "",
    ]
    for name, table in report["signals"].items():
        lines += list(render_signals(name, table))
    lines += [
        "## The news classifier",
        "",
        "Signals the classifier already has as features (measured at the `cutoff` moment, "
        "over the burst's human edits): "
        + ", ".join(f"`{s}` (feature `{f}`)" for s, f in CLASSIFIER_FEATURES.items())
        + ". Not among its features: "
        + ", ".join(f"`{s}`" for s in SIGNALS if s not in CLASSIFIER_FEATURES)
        + ".",
        "",
    ]
    rules = report.get("rules")
    if rules:
        current = report["current_max_anon_share"]
        lines += ["## Candidate rule: `max_anon_share` in the detector", ""]
        lines += list(render_rules(rules, current))
        lines += list(render_dropped(rules, current))
    return "\n".join(lines).rstrip() + "\n"


def write_report(report: dict[str, Any], results_dir: Path, day: date) -> tuple[Path, Path]:
    results_dir.mkdir(parents=True, exist_ok=True)
    base = results_dir / f"{day.isoformat()}_vandalism"
    json_path, md_path = base.with_suffix(".json"), base.with_suffix(".md")
    with json_path.open("w", encoding="utf-8", newline="\n") as out:
        out.write(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n")
    with md_path.open("w", encoding="utf-8", newline="\n") as out:
        out.write(render_markdown(report))
    return json_path, md_path


# --- Command line ---------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS_PATH)
    parser.add_argument("--gate-labels", type=Path, default=gate.DEFAULT_LABELS)
    parser.add_argument("--fixture", type=Path, default=gate.DEFAULT_FIXTURE)
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--date", type=date.fromisoformat, help="report date (default: today)")
    parser.add_argument("--resamples", type=int, default=DEFAULT_RESAMPLES)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--no-rules", action="store_true", help="skip the detector runs (signals only)"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging()
    logging.getLogger("wikipulse.detect").setLevel(logging.WARNING)  # a line per spike
    inputs = Inputs(
        args.labels, args.gate_labels, args.fixture, args.resamples, args.seed, not args.no_rules
    )
    report = analyse(inputs, get_settings())
    day = args.date or datetime.now(UTC).date()  # the report's name only
    json_path, md_path = write_report(report, args.results_dir, day)
    logger.info("Wrote %s and %s", md_path, json_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

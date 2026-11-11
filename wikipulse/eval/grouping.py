"""Evaluate event grouping with pairwise precision and recall.

    uv run python -m wikipulse.eval.grouping label  --run-id 03b-day --day 2026-11-05
    uv run python -m wikipulse.eval.grouping label  --run-id 03b-day --day 2026-11-05 --manual
    uv run python -m wikipulse.eval.grouping report --run-id 03b-day --day 2026-11-05

**Pairwise metrics.** Every pair of the day's spikes is either put in the same event by
the grouper ("together") or not; and is either the same story or not (the label).
- precision = of the pairs put together, how many are the same story;
- recall = of the same-story pairs, how many were put together.

**Why sampling, and why strata.** 665 spikes make 220,000 pairs, almost all of them
obviously different stories. A plain random sample would contain almost no
same-story pairs. So the pairs are split into three *strata* and each is sampled
separately (`STRATA`):
- `together`: pairs the run put in one event (gives precision directly);
- `near`: pairs not together, but linked or with similar summaries (cosine at least
  `NEAR_COSINE`): where the grouper's misses are most likely;
- `far`: all other pairs.
Each labeled pair stands for `population / labeled` pairs of its stratum
(stratified estimation), which gives estimated counts of same-story pairs, and from
them recall. Samples use a fixed seed and a hash order, so another run over the same
spikes reuses most labels.
"""

import argparse
import hashlib
import json
import logging
import math
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import mean, median
from typing import Any

import numpy as np
import psycopg

from wikipulse.config import EventsConfig, Settings, get_settings
from wikipulse.db.events import SpikeRow, event_membership, run_info, spikes_with_pages
from wikipulse.eval.detection import git_commit, parse_overrides
from wikipulse.eval.grouping_labels import (
    DEFAULT_PAIR_LABELS_PATH,
    LLMPairLabeler,
    PageView,
    PairKey,
    PairLabelRecord,
    PairToLabel,
    label_pairs_manually,
    label_pairs_with_llm,
    labels_by_pair,
    load_pair_records,
    pair_key,
)
from wikipulse.eval.metrics import day_start
from wikipulse.events.embed import Embedder
from wikipulse.events.run import to_pages
from wikipulse.events.similarity import SpikePage
from wikipulse.llm import get_client
from wikipulse.logging_setup import setup_logging

logger = logging.getLogger(__name__)

STRATA = ("together", "near", "far")
DEFAULT_SIZES = {"together": 50, "near": 50, "far": 25}
NEAR_COSINE = 0.65  # about the 99th percentile of summary similarity between spikes
DEFAULT_SEED = 2026
DEFAULT_RESULTS_DIR = Path("evals") / "results"


# --- Pairs and strata ---------------------------------------------------------------


@dataclass(frozen=True)
class Pair:
    i: int  # indexes into the day's spike list, i < j
    j: int
    stratum: str


def linked(a: SpikePage, b: SpikePage) -> bool:
    """True if either page links to the other."""
    return not (a.links.isdisjoint(b.names) and b.links.isdisjoint(a.names))


def stratify(
    pages: Sequence[SpikePage], event_of: dict[int, int], near_cosine: float = NEAR_COSINE
) -> list[Pair]:
    """Every unordered pair of spikes, with its stratum."""
    vectors = np.stack([p.embedding for p in pages]) if pages else np.zeros((0, 1))
    cosines = vectors @ vectors.T
    pairs = []
    for i in range(len(pages)):
        for j in range(i + 1, len(pages)):
            a, b = pages[i], pages[j]
            together = event_of.get(a.spike_id) is not None and event_of.get(
                a.spike_id
            ) == event_of.get(b.spike_id)
            if together:
                stratum = "together"
            elif linked(a, b) or cosines[i, j] >= near_cosine:
                stratum = "near"
            else:
                stratum = "far"
            pairs.append(Pair(i, j, stratum))
    return pairs


def spike_key(page: SpikePage) -> tuple[str, datetime]:
    """A spike's identity across runs: title and start."""
    return (page.title, page.started_at)


def key_of(pair: Pair, pages: Sequence[SpikePage]) -> PairKey:
    """The label key of a page pair."""
    return pair_key(spike_key(pages[pair.i]), spike_key(pages[pair.j]))


def sample_order(key: PairKey, seed: int) -> str:
    """A pseudo-random but fixed rank for a pair: the same pair always ranks the same,
    whichever run it is sampled for, so runs over the same spikes share labels."""
    text = f"{seed}|{key[0][0]}|{key[0][1].isoformat()}|{key[1][0]}|{key[1][1].isoformat()}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sample_pairs(
    pairs: Sequence[Pair], pages: Sequence[SpikePage], sizes: dict[str, int], seed: int
) -> list[Pair]:
    """The first `sizes[stratum]` pairs of each stratum in hash order."""
    sample = []
    for stratum in STRATA:
        members = [p for p in pairs if p.stratum == stratum]
        members.sort(key=lambda p: sample_order(key_of(p, pages), seed))
        sample.extend(members[: sizes.get(stratum, 0)])
    return sample


# --- Metrics ------------------------------------------------------------------------


@dataclass
class StratumCount:
    """One stratum: how many pairs it has, and what the labels of its sample say."""

    name: str
    population: int
    sampled: int = 0
    same: int = 0
    different: int = 0
    unsure: int = 0

    @property
    def labeled(self) -> int:
        return self.same + self.different

    @property
    def same_rate(self) -> float | None:
        return self.same / self.labeled if self.labeled else None

    @property
    def estimated_same(self) -> float | None:
        """Estimated same-story pairs in the whole stratum."""
        rate = self.same_rate
        return None if rate is None else rate * self.population


@dataclass
class GroupingMetrics:
    strata: dict[str, StratumCount] = field(default_factory=dict)

    @property
    def precision(self) -> float | None:
        return self.strata["together"].same_rate

    @property
    def precision_margin(self) -> float | None:
        """Half-width of a 95% normal interval around the precision."""
        stratum = self.strata["together"]
        p = stratum.same_rate
        return None if p is None else 1.96 * math.sqrt(p * (1 - p) / stratum.labeled)

    def recall(self, include_far: bool = True) -> float | None:
        """Estimated share of same-story pairs that were put together."""
        found = self.strata["together"].estimated_same
        missed_strata = ["near", "far"] if include_far else ["near"]
        missed = [self.strata[name].estimated_same for name in missed_strata]
        if found is None or any(m is None for m in missed):
            return None
        total = found + sum(missed)
        return found / total if total else None

    def labeled_pairs_recall(self) -> float | None:
        """Recall over the labeled pairs only, without weights (biased; for contrast)."""
        same = sum(s.same for s in self.strata.values())
        return self.strata["together"].same / same if same else None

    def far_upper_bound(self) -> float | None:
        """If no far pair was labeled same: a 95% upper bound on same-story far pairs
        ("rule of three": 3 / n), as a count."""
        far = self.strata["far"]
        if far.same or not far.labeled:
            return None
        return 3 / far.labeled * far.population


def count_strata(
    pairs: Sequence[Pair],
    sample: Sequence[Pair],
    pages: Sequence[SpikePage],
    labels: dict[PairKey, PairLabelRecord],
) -> GroupingMetrics:
    """Sampled and labeled pairs counted per stratum."""
    population = Counter(p.stratum for p in pairs)
    metrics = GroupingMetrics({name: StratumCount(name, population[name]) for name in STRATA})
    for pair in sample:
        stratum = metrics.strata[pair.stratum]
        stratum.sampled += 1
        label = labels.get(key_of(pair, pages))
        if label is not None:
            setattr(stratum, label.label, getattr(stratum, label.label) + 1)
    return metrics


def event_sizes(pages: Sequence[SpikePage], event_of: dict[int, int]) -> list[int]:
    """Distinct pages per event (a page that spiked twice counts once), over the day's
    spikes only."""
    titles: dict[int, set[str]] = {}
    for p in pages:
        if p.spike_id in event_of:
            titles.setdefault(event_of[p.spike_id], set()).add(p.canonical_title)
    return sorted((len(t) for t in titles.values()), reverse=True)


# --- Loading ------------------------------------------------------------------------


@dataclass
class DayData:
    spike_run_id: str
    rows: list[SpikeRow]
    pages: list[SpikePage]
    event_of: dict[int, int]
    pairs: list[Pair]

    def view(self, index: int) -> PageView:
        row = self.rows[index]
        return PageView(
            title=row.title,
            canonical_title=row.canonical_title or row.title,
            started_at=row.started_at,
            total_edits=row.total_edits,
            description=row.description,
            summary=row.summary,
        )

    def to_label(self, pair: Pair) -> PairToLabel:
        return PairToLabel(self.view(pair.i), self.view(pair.j), pair.stratum)


def load_day(settings: Settings, run_id: str, day: date) -> DayData:
    """The day's spikes (with pages and embeddings), the run's events and all pairs."""
    with psycopg.connect(settings.postgres_dsn) as conn:
        spike_run_id, _, _ = run_info(conn, run_id)
        if spike_run_id is None:
            raise SystemExit(f"Grouping run {run_id!r} has no events; run wikipulse.events.run")
        start = day_start(day)
        rows = spikes_with_pages(conn, spike_run_id, start, start + timedelta(days=1))
        event_of = event_membership(conn, run_id)
    pages = to_pages(rows, Embedder(settings.events_embed_model))
    pairs = stratify(pages, event_of)
    logger.info("%d spikes of run %s on %s: %d pairs", len(pages), spike_run_id, day, len(pairs))
    return DayData(spike_run_id, rows, pages, event_of, pairs)


# --- Report -------------------------------------------------------------------------


def pct(value: float | None) -> str:
    """A share as a percentage with one decimal, or `n/a`."""
    return "n/a" if value is None else f"{100 * value:.1f}%"


def build_report(
    name: str,
    run_id: str,
    day: date,
    data: DayData,
    sample: Sequence[Pair],
    labels: dict[PairKey, PairLabelRecord],
    config: dict[str, Any],
    now: datetime,
    note: str = "",
) -> dict[str, Any]:
    """The whole grouping report as a JSON-ready dict."""
    metrics = count_strata(data.pairs, sample, data.pages, labels)
    sizes = event_sizes(data.pages, data.event_of)
    sampled_pairs = []
    for pair in sample:
        label = labels.get(key_of(pair, data.pages))
        a, b = data.pages[pair.i], data.pages[pair.j]
        sampled_pairs.append(
            {
                "stratum": pair.stratum,
                "a": a.canonical_title,
                "b": b.canonical_title,
                "a_started_at": a.started_at.isoformat(),
                "b_started_at": b.started_at.isoformat(),
                "label": label.label if label else None,
                "labeler": label.labeler if label else None,
                "reason": label.reason if label else "",
            }
        )
    return {
        "name": name,
        "grouping_run": run_id,
        "spike_run": data.spike_run_id,
        "day": day.isoformat(),
        "created_at": now.isoformat(),
        "git": git_commit(),
        "note": note,
        "config": config,
        "events": {
            "spikes": len(data.pages),
            "pages": len({p.canonical_title for p in data.pages}),
            "events": len(sizes),
            "pages_per_event_mean": round(mean(sizes), 3) if sizes else None,
            "pages_per_event_median": median(sizes) if sizes else None,
            "pages_per_event_max": max(sizes) if sizes else None,
            "multi_page_events": sum(1 for s in sizes if s > 1),
            "pages_in_multi_page_events": sum(s for s in sizes if s > 1),
        },
        "strata": {
            name: {
                "population": s.population,
                "sampled": s.sampled,
                "same": s.same,
                "different": s.different,
                "unsure": s.unsure,
                "unlabeled": s.sampled - s.labeled - s.unsure,
                "estimated_same": s.estimated_same,
            }
            for name, s in metrics.strata.items()
        },
        "metrics": {
            "precision": metrics.precision,
            "precision_margin_95": metrics.precision_margin,
            "recall": metrics.recall(include_far=True),
            "recall_candidates": metrics.recall(include_far=False),
            "recall_labeled_pairs": metrics.labeled_pairs_recall(),
            "far_same_upper_bound_95": metrics.far_upper_bound(),
        },
        "pairs": sampled_pairs,
    }


def render_markdown(report: dict[str, Any]) -> str:
    """The grouping report as Markdown."""
    m, e = report["metrics"], report["events"]
    margin = m["precision_margin_95"]
    lines = [
        f"# Grouping evaluation: {report['name']}",
        "",
        f"- Grouping run `{report['grouping_run']}` over spike run `{report['spike_run']}`, "
        f"spikes started on {report['day']} (UTC)",
        f"- Created {report['created_at'][:19]} UTC, commit {report['git']['commit']}"
        + (" (uncommitted changes)" if report["git"]["dirty"] else ""),
    ]
    if report["note"]:
        lines.append(f"- Note: {report['note']}")
    lines += [
        "",
        "## Events",
        "",
        f"{e['spikes']} spikes of {e['pages']} distinct pages in {e['events']} events; "
        f"distinct pages per event: mean "
        f"{e['pages_per_event_mean']}, median {e['pages_per_event_median']}, max "
        f"{e['pages_per_event_max']}. {e['multi_page_events']} events have more than one "
        f"page ({e['pages_in_multi_page_events']} pages).",
        "",
        "## Pairwise metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Precision (labeled `together` pairs) | {pct(m['precision'])}"
        + (f" ± {100 * margin:.1f} pp" if margin is not None else "")
        + " |",
        f"| Recall, estimated over all pairs | {pct(m['recall'])} |",
        f"| Recall, estimated over candidate pairs (together + near) | "
        f"{pct(m['recall_candidates'])} |",
        f"| Recall over labeled pairs, unweighted (biased, for contrast) | "
        f"{pct(m['recall_labeled_pairs'])} |",
        "",
        "| Stratum | Pairs | Sampled | Same | Different | Unsure | Est. same pairs |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, s in report["strata"].items():
        est = "n/a" if s["estimated_same"] is None else f"{s['estimated_same']:.0f}"
        lines.append(
            f"| {name} | {s['population']} | {s['sampled']} | {s['same']} | "
            f"{s['different']} | {s['unsure']} | {est} |"
        )
    if m["far_same_upper_bound_95"] is not None:
        lines += [
            "",
            f"No `far` pair was labeled same; with 95% confidence the `far` stratum holds at "
            f"most about {m['far_same_upper_bound_95']:.0f} same-story pairs (rule of three).",
        ]
    lines += ["", "## Configuration", "", "```json", json.dumps(report["config"], indent=2)]
    lines += ["```", "", "## Disagreements", ""]
    wrong = [
        p
        for p in report["pairs"]
        if (p["stratum"] == "together" and p["label"] == "different")
        or (p["stratum"] != "together" and p["label"] == "same")
    ]
    for p in wrong:
        kind = "false merge" if p["stratum"] == "together" else f"missed ({p['stratum']})"
        lines.append(f"- {kind}: *{p['a']}* / *{p['b']}*: {p['reason']}")
    if not wrong:
        lines.append("None among the labeled pairs.")
    return "\n".join(lines) + "\n"


def summary_line(report: dict[str, Any]) -> str:
    """The report's main numbers in one line."""
    m, e = report["metrics"], report["events"]
    return (
        f"{e['events']} events from {e['spikes']} spikes; pairwise precision "
        f"{pct(m['precision'])}, recall {pct(m['recall'])} "
        f"(candidates {pct(m['recall_candidates'])})"
    )


# --- Command line -------------------------------------------------------------------


def parse_sizes(value: str) -> dict[str, int]:
    """`together=50,near=50,far=25`."""
    sizes = dict(DEFAULT_SIZES)
    for name, number in parse_overrides([value]).items():
        if name not in STRATA:
            raise argparse.ArgumentTypeError(f"unknown stratum {name!r}")
        sizes[name] = int(number)
    return sizes


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("label", "report"):
        p = sub.add_parser(command)
        p.add_argument("--run-id", required=True, help="grouping run to evaluate")
        p.add_argument("--day", type=date.fromisoformat, required=True, help="UTC day")
        p.add_argument("--sizes", type=parse_sizes, default=dict(DEFAULT_SIZES))
        p.add_argument("--seed", type=int, default=DEFAULT_SEED)
        p.add_argument("--labels", type=Path, default=DEFAULT_PAIR_LABELS_PATH)
    label = sub.choices["label"]
    label.add_argument("--manual", action="store_true", help="label yourself in the terminal")
    label.add_argument("--model", help="LLM as backend:model (default: EVAL_LABEL_MODEL)")
    report = sub.choices["report"]
    report.add_argument("--name", help="report name (default: the run id)")
    report.add_argument("--set", action="append", default=[], help="EVENTS_* used, e.g. k=v")
    report.add_argument("--note", default="")
    report.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    return parser.parse_args(argv)


def run_label(args: argparse.Namespace, settings: Settings, data: DayData) -> None:
    """The `label` command: label the sampled pairs not labeled yet."""
    sample = sample_pairs(data.pairs, data.pages, args.sizes, args.seed)
    pairs = [data.to_label(pair) for pair in sample]

    def now() -> datetime:
        return datetime.now(UTC)  # label metadata only

    if args.manual:
        label_pairs_manually(pairs, data.spike_run_id, args.run_id, args.labels, now)
        return
    model_setting = args.model or settings.eval_label_model
    labeler = LLMPairLabeler(get_client(model_setting, settings))
    try:
        done = label_pairs_with_llm(
            pairs, labeler, model_setting, data.spike_run_id, args.run_id, args.labels, now
        )
    finally:
        labeler.close()
    logger.info("Labeled %d of %d sampled pairs with %s", done, len(pairs), model_setting)


def run_report(args: argparse.Namespace, settings: Settings, data: DayData) -> Path:
    """The `report` command: write the grouping report; returns its path."""
    sample = sample_pairs(data.pairs, data.pages, args.sizes, args.seed)
    labels = labels_by_pair(load_pair_records(args.labels))
    base = settings.events_config().model_dump()
    config = EventsConfig.model_validate({**base, **parse_overrides(args.set)}).model_dump()
    name = args.name or args.run_id
    now = datetime.now(UTC)  # report metadata only
    report = build_report(name, args.run_id, args.day, data, sample, labels, config, now, args.note)
    args.results_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{now:%Y-%m-%d}_{name}"
    (args.results_dir / f"{stem}.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    md_path = args.results_dir / f"{stem}.md"
    md_path.write_text(render_markdown(report), encoding="utf-8", newline="\n")
    print(summary_line(report))
    return md_path


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    setup_logging()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    data = load_day(settings, args.run_id, args.day)
    if args.command == "label":
        run_label(args, settings, data)
    else:
        print(f"Report: {run_report(args, settings, data)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

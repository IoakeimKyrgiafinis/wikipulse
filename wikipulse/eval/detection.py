"""Evaluate spike detection against Wikipedia's "Current events" portal.

    uv run python -m wikipulse.eval.detection --from 2026-11-06 --to 2026-11-07 --name baseline
    uv run python -m wikipulse.eval.detection --compare baseline loose

Reads the spikes of one detector run (`--run-id`, default: the `--name`) from Postgres.
Precision, labels and time to detection use the spikes that *started* between `--from`
and `--to` (inclusive, UTC days); recall may also use spikes from the days around them,
since a portal day's story can spike the day before or after. It fetches the portal pages
(cached in `data/ground_truth/`), reads the labels (`evals/labels/spike_labels.jsonl`),
and writes `evals/results/<today>_<name>.md` and `.json`. `--compare A B` prints how the
metrics of two saved reports differ.

The detector's thresholds are not stored with its spikes, so the report's config
snapshot is the `DETECT_*` configuration of *this* process (plus `--set` changes): run the
evaluation with the same settings as the detector run.
"""

import argparse
import json
import logging
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

from wikipulse.config import DetectorConfig, Settings, get_settings
from wikipulse.db.spikes import spikes_for_run
from wikipulse.detect.detector import SpikeRecord
from wikipulse.eval.ground_truth import PortalDay, fetch_portal_day
from wikipulse.eval.label_store import DEFAULT_LABELS_PATH, load_labels
from wikipulse.eval.metrics import (
    DEFAULT_SAMPLE_SIZE,
    DEFAULT_SEED,
    PRECISION_WINDOW_H,
    RECALL_WINDOW_H,
    DayRecall,
    Label,
    SpikeMatch,
    by_title,
    day_start,
    label_precision,
    match_spikes,
    pick_label,
    pooled_recall,
    portal_precision,
    recall_for_day,
    sample_spikes,
    spike_end,
    time_to_detection,
)
from wikipulse.eval.wiki_client import WikiClient
from wikipulse.logging_setup import setup_logging

logger = logging.getLogger(__name__)

DEFAULT_RESULTS_DIR = Path("evals") / "results"
# Portal days fetched around the evaluated days, so spikes near the edges can match.
PORTAL_MARGIN_DAYS = 2
# Spikes loaded around the evaluated days, for recall only.
SPIKE_MARGIN_DAYS = 2

# Metrics shown by --compare, in order: (key in "metrics", label, is a share).
COMPARED_METRICS = [
    ("spikes", "spikes", False),
    ("recall_all", "recall, all links", True),
    ("recall_lead", "recall, lead links", True),
    ("portal_precision", "portal precision (lower bound)", True),
    ("label_precision", "label precision", True),
    ("labeled_spikes", "labeled spikes", False),
    ("ttd_median_min", "time to detection, median (min)", False),
    ("ttd_p90_min", "time to detection, p90 (min)", False),
]


def days_between(first: date, last: date) -> list[date]:
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]


def git_commit() -> dict[str, Any]:
    """The current commit hash, and whether the working tree has uncommitted changes."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "dirty": None}
    return {"commit": commit, "dirty": bool(status.strip())}


def parse_overrides(items: list[str]) -> dict[str, str]:
    """`["min_edits=4", "min_editors=2"]` -> {"min_edits": "4", "min_editors": "2"}."""
    overrides = {}
    for item in items:
        for part in item.split(","):
            if part:
                key, _, value = part.partition("=")
                overrides[key.strip()] = value.strip()
    return overrides


def config_snapshot(settings: Settings, overrides: dict[str, str]) -> dict[str, Any]:
    base = settings.detector_config().model_dump()
    return DetectorConfig.model_validate({**base, **overrides}).model_dump()


# --- Building the report ------------------------------------------------------------


def load_portal(
    client: WikiClient, api_url: str, first: date, last: date, today: date
) -> list[PortalDay]:
    """Portal days from `first` to `last`; days after `today` can't exist yet."""
    return [
        fetch_portal_day(client, api_url, day) if day <= today else PortalDay(day, [], [], {}, True)
        for day in days_between(first, last)
    ]


def spike_row(match: SpikeMatch, labels: list[Label]) -> dict[str, Any]:
    spike = match.spike
    chosen = pick_label([label for label in labels if label.covers(spike)])
    return {
        "title": spike.title,
        "status": spike.status,
        "first_edit_at": spike.first_edit_at.isoformat(),
        "started_at": spike.started_at.isoformat(),
        "end_at": spike_end(spike).isoformat(),
        "total_edits": spike.total_edits,
        "distinct_editors": spike.distinct_editors,
        "revert_edits": spike.revert_edits,
        "peak_edits_10m": spike.peak_edits_10m,
        "score": round(spike.score, 2),
        "portal_days": [day.isoformat() for day in match.portal_days],
        "label": chosen.label if chosen else None,
        "labeler": chosen.labeler if chosen else None,
    }


def day_row(
    day: date,
    all_recall: DayRecall,
    lead_recall: DayRecall,
    matches: list[SpikeMatch],
    sample: list[SpikeRecord],
    labels: list[Label],
    portal: PortalDay,
) -> dict[str, Any]:
    """Per-day breakdown. Spikes belong to the day they started on."""

    def started_today(s: SpikeRecord) -> bool:
        return s.started_at.astimezone(UTC).date() == day

    todays = [m for m in matches if started_today(m.spike)]
    labeled = label_precision([s for s in sample if started_today(s)], labels)
    return {
        "day": day.isoformat(),
        "portal_missing": portal.missing,
        "links": len(all_recall.articles),
        "links_found": len(all_recall.found),
        "recall_all": all_recall.recall,
        "leads": len(lead_recall.articles),
        "leads_found": len(lead_recall.found),
        "recall_lead": lead_recall.recall,
        "spikes": len(todays),
        "spikes_on_portal": sum(m.matched for m in todays),
        "portal_precision": portal_precision(todays),
        "labeled_spikes": labeled.labeled,
        "label_precision": labeled.precision,
        "found_leads": lead_recall.found,
        "found_links": all_recall.found,
    }


def build_report(
    name: str,
    run_id: str,
    first: date,
    last: date,
    spikes: list[SpikeRecord],
    portal: list[PortalDay],
    labels: list[Label],
    config: dict[str, Any],
    git: dict[str, Any],
    created_at: datetime,
    note: str = "",
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """Every number of the evaluation, as a JSON-ready dict (pure: no I/O).

    `spikes` may include spikes from around the evaluated days: they count for recall
    only. Everything else uses the spikes that started from `first` to `last`.
    """
    index = by_title(spikes)
    evaluated = [p for p in portal if first <= p.day <= last]
    all_recall = [recall_for_day(p, index) for p in evaluated]
    lead_recall = [recall_for_day(p, index, leads_only=True) for p in evaluated]
    in_range = [s for s in spikes if first <= s.started_at.astimezone(UTC).date() <= last]
    matches = match_spikes(in_range, portal)
    delay = time_to_detection(matches)
    sample = sample_spikes(in_range, sample_size, seed)
    labeled = label_precision(sample, labels)
    metrics = {
        "spikes": len(in_range),
        "spikes_on_portal": sum(m.matched for m in matches),
        "portal_precision": portal_precision(matches),
        "links": sum(len(d.articles) for d in all_recall),
        "links_found": sum(len(d.found) for d in all_recall),
        "recall_all": pooled_recall(all_recall),
        "leads": sum(len(d.articles) for d in lead_recall),
        "leads_found": sum(len(d.found) for d in lead_recall),
        "recall_lead": pooled_recall(lead_recall),
        "label_sample": len(sample),
        "label_seed": seed,
        "labeled_spikes": labeled.labeled,
        "label_counts": labeled.counts,
        "labelers": labeled.labelers,
        "label_models": labeled.models,
        "label_precision": labeled.precision,
        "ttd_count": delay.count,
        "ttd_median_min": delay.median,
        "ttd_p90_min": delay.p90,
        "ttd_min_min": delay.minimum,
        "ttd_max_min": delay.maximum,
    }
    return {
        "name": name,
        "run_id": run_id,
        "created_at": created_at.isoformat(),
        "note": note,
        "date_from": first.isoformat(),
        "date_to": last.isoformat(),
        "git": git,
        "config": config,
        "windows_h": {"recall": RECALL_WINDOW_H, "precision": PRECISION_WINDOW_H},
        "metrics": metrics,
        "per_day": [
            day_row(p.day, a, b, matches, sample, labels, p)
            for p, a, b in zip(evaluated, all_recall, lead_recall, strict=True)
        ],
        "portal_days": [
            {"day": p.day.isoformat(), "missing": p.missing, "links": p.links, "leads": p.leads}
            for p in portal
        ],
        "spikes": [spike_row(m, labels) for m in matches],
    }


# --- Markdown -------------------------------------------------------------------------


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def num(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def render_markdown(report: dict[str, Any]) -> str:
    m = report["metrics"]
    git = report["git"]
    commit = git["commit"] or "unknown"
    if git.get("dirty"):
        commit += " (with uncommitted changes)"
    labelers = ", ".join(f"{k}: {v}" for k, v in sorted(m["labelers"].items())) or "none"
    models = ", ".join(f"{k} ({v})" for k, v in sorted(m["label_models"].items()))
    counts = m["label_counts"]
    lines = [
        f"# Detection evaluation: {report['name']}",
        "",
        f"- Detector run: `{report['run_id']}`",
        f"- Days: {report['date_from']} to {report['date_to']} (UTC)",
        f"- Created: {report['created_at']}",
        f"- Git commit: `{commit}`",
        *([f"- Note: {report['note']}"] if report.get("note") else []),
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|--------|------:|",
        f"| Spikes | {m['spikes']} |",
        f"| Recall, all portal links | {pct(m['recall_all'])} "
        f"({m['links_found']} of {m['links']}) |",
        f"| Recall, lead links | {pct(m['recall_lead'])} ({m['leads_found']} of {m['leads']}) |",
        f"| Precision against the portal (**lower bound**) | {pct(m['portal_precision'])} "
        f"({m['spikes_on_portal']} of {m['spikes']}) |",
        f"| Precision from labels (**estimate**) | {pct(m['label_precision'])} "
        f"({counts.get('news', 0)} news, {counts.get('not news', 0)} not news, "
        f"{counts.get('unsure', 0)} unsure) |",
        f"| Time to detection, median | {num(m['ttd_median_min'])} min |",
        f"| Time to detection, 90th percentile | {num(m['ttd_p90_min'])} min |",
        "",
        "How to read these numbers:",
        "",
        f"- **Recall** is the share of articles linked from a day's portal page that had a "
        f"spike within {RECALL_WINDOW_H:.0f} hours of that day. Many links are generic "
        "(countries, cities, organizations), so recall over *lead* links (the first link "
        "of each portal entry, usually its subject) is the more meaningful of the two.",
        f"- **Precision against the portal** is the share of spikes whose article is on a "
        f"portal page within {PRECISION_WINDOW_H:.0f} hours. It is only a *lower bound*: "
        "much genuine news (sports, TV, deaths of minor figures) never reaches the portal.",
        f"- **Precision from labels** is measured on this run's random sample of "
        f"{m['label_sample']} spikes (seed {m['label_seed']}, as drawn by "
        f"`wikipulse.eval.label`); {m['labeled_spikes']} of them have a label "
        f"(labelers: {labelers}{'; models: ' + models if models else ''}). "
        "News share among news and not-news labels, `unsure` left out. LLM labels are only "
        "an *estimate* of the truth; a human label always replaces an LLM label.",
        f"- **Time to detection** is the time from the first edit of the burst to the "
        f"spike's start, over the {m['ttd_count']} spikes found on the portal "
        f"(min {num(m['ttd_min_min'])}, max {num(m['ttd_max_min'])} minutes).",
        "",
        "## Per day",
        "",
        "Spikes are counted on the day they started.",
        "",
        "| Day | Portal links | Recall (all) | Leads | Recall (lead) | Spikes | On portal "
        "| Portal precision | Labeled | Label precision |",
        "|-----|------:|------:|------:|------:|------:|------:|------:|------:|------:|",
    ]
    for d in report["per_day"]:
        links = "missing" if d["portal_missing"] else str(d["links"])
        lines.append(
            f"| {d['day']} | {links} | {pct(d['recall_all'])} | {d['leads']} "
            f"| {pct(d['recall_lead'])} | {d['spikes']} | {d['spikes_on_portal']} "
            f"| {pct(d['portal_precision'])} | {d['labeled_spikes']} "
            f"| {pct(d['label_precision'])} |"
        )
    lines += ["", "Lead links found:", ""]
    for d in report["per_day"]:
        found = ", ".join(d["found_leads"]) or "none"
        lines.append(f"- {d['day']}: {found}")
    lines += ["", "## Spikes on the portal", ""]
    on_portal = [s for s in report["spikes"] if s["portal_days"]]
    for s in on_portal:
        lines.append(
            f"- {s['title']} (started {s['started_at'][:16]}, portal "
            f"{', '.join(s['portal_days'])}, label {s['label'] or '-'})"
        )
    if not on_portal:
        lines.append("None.")
    lines += [
        "",
        "## Detector configuration",
        "",
        "As configured for this evaluation (`DETECT_*` settings and `--set` changes).",
        "",
        "| Setting | Value |",
        "|---------|------:|",
    ]
    lines += [f"| {key} | {value} |" for key, value in report["config"].items()]
    lines += ["", "All spikes, portal links and labels are in the `.json` file next to this one."]
    return "\n".join(lines) + "\n"


# --- Comparing two reports ------------------------------------------------------------


def find_report(name_or_path: str, results_dir: Path) -> Path:
    """A report's JSON: a path, or the newest `<date>_<name>.json` in `results_dir`."""
    path = Path(name_or_path)
    if path.suffix == ".json" and path.exists():
        return path
    candidates = sorted(results_dir.glob(f"*_{name_or_path}.json"))
    if not candidates:
        raise FileNotFoundError(f"No report named {name_or_path!r} in {results_dir}")
    return candidates[-1]


def compare_reports(a: dict[str, Any], b: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    """Rows of (metric, value A, value B, B minus A); shares in percentage points."""
    rows = []
    for key, label, is_share in COMPARED_METRICS:
        value_a, value_b = a["metrics"].get(key), b["metrics"].get(key)
        show = pct if is_share else num
        if value_a is None or value_b is None:
            diff = "n/a"
        elif is_share:
            diff = f"{100 * (value_b - value_a):+.1f} pp"
        else:
            diff = f"{value_b - value_a:+.1f}"
        rows.append((label, show(value_a), show(value_b), diff))
    changed = [
        (key, a["config"].get(key), b["config"].get(key))
        for key in sorted(set(a["config"]) | set(b["config"]))
        if a["config"].get(key) != b["config"].get(key)
    ]
    for key, value_a, value_b in changed:
        rows.append((f"config: {key}", str(value_a), str(value_b), ""))
    return rows


def print_comparison(path_a: Path, path_b: Path) -> None:
    a = json.loads(path_a.read_text(encoding="utf-8"))
    b = json.loads(path_b.read_text(encoding="utf-8"))
    rows = compare_reports(a, b)
    header = ("metric", a["name"], b["name"], "difference")
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(4)]
    for row in [header, *rows]:
        print("  ".join(str(cell).ljust(width) for cell, width in zip(row, widths, strict=True)))


# --- Command line -------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="date_from", type=date.fromisoformat)
    parser.add_argument("--to", dest="date_to", type=date.fromisoformat)
    parser.add_argument("--name", help="name of this evaluation (and of the report files)")
    parser.add_argument("--run-id", help="detector run to evaluate (default: --name)")
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        help="detector settings the run used beyond DETECT_*, e.g. min_edits=4,min_editors=2",
    )
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS_PATH)
    parser.add_argument(
        "--label-size", type=int, default=DEFAULT_SAMPLE_SIZE, help="label sample size"
    )
    parser.add_argument("--label-seed", type=int, default=DEFAULT_SEED, help="label sample seed")
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--note", default="", help="free text for the report, e.g. data coverage")
    parser.add_argument("--compare", nargs=2, metavar=("RUN_A", "RUN_B"))
    args = parser.parse_args(argv)
    if not args.compare and not (args.date_from and args.date_to and args.name):
        parser.error("--from, --to and --name are required (or use --compare A B)")
    if args.date_from and args.date_to and args.date_to < args.date_from:
        parser.error("--to is before --from")
    return args


def evaluate(args: argparse.Namespace, settings: Settings) -> Path:
    """Run the evaluation and write the two report files; returns the Markdown path."""
    run_id = args.run_id or args.name
    first, last = args.date_from, args.date_to
    config = config_snapshot(settings, parse_overrides(args.set))
    with psycopg.connect(settings.postgres_dsn) as conn:
        # Recall looks up to RECALL_WINDOW_H (plus a spike's duration) around each day.
        margin = timedelta(days=SPIKE_MARGIN_DAYS)
        spikes = spikes_for_run(
            conn, run_id, day_start(first) - margin, day_start(last) + timedelta(days=1) + margin
        )
    logger.info("Run %s: %d spikes loaded around %s to %s", run_id, len(spikes), first, last)
    now = datetime.now(UTC)  # report metadata and "which portal days can exist", not logic
    margin = timedelta(days=PORTAL_MARGIN_DAYS)
    cache_dir = settings.data_dir / "ground_truth"
    with WikiClient(settings.user_agent, cache_dir, settings.wiki_api_min_interval_s) as client:
        portal = load_portal(
            client, settings.wiki_api_url, first - margin, last + margin, now.date()
        )
        logger.info("Portal pages: %d requests, %d from cache", client.requests, client.cache_hits)
    labels = load_labels(args.labels)
    report = build_report(
        args.name,
        run_id,
        first,
        last,
        spikes,
        portal,
        labels,
        config,
        git_commit(),
        now,
        args.note,
        args.label_size,
        args.label_seed,
    )
    args.results_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{now:%Y-%m-%d}_{args.name}"
    json_path = args.results_dir / f"{stem}.json"
    md_path = args.results_dir / f"{stem}.md"
    json_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    md_path.write_text(render_markdown(report), encoding="utf-8", newline="\n")
    return md_path


def summary_line(report: dict[str, Any]) -> str:
    m = report["metrics"]
    return (
        f"{m['spikes']} spikes; recall {pct(m['recall_all'])} (lead {pct(m['recall_lead'])}); "
        f"portal precision >= {pct(m['portal_precision'])}; "
        f"label precision {pct(m['label_precision'])} on {m['labeled_spikes']}; "
        f"time to detection median {num(m['ttd_median_min'])} min, "
        f"p90 {num(m['ttd_p90_min'])} min"
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    setup_logging()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # titles on Windows consoles
    if args.compare:
        a, b = (find_report(name, args.results_dir) for name in args.compare)
        print_comparison(a, b)
        return 0
    md_path = evaluate(args, settings)
    json_path = md_path.parent / (md_path.name.removesuffix(".md") + ".json")
    report = json.loads(json_path.read_text(encoding="utf-8"))
    print(f"Report: {md_path}")
    print(summary_line(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())

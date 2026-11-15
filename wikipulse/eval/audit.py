"""Audit the LLM labelers and judges against human labels (spec 12b).

    uv run python -m wikipulse.eval.audit
    uv run python -m wikipulse.eval.audit --date 2026-11-15 --resamples 2000

Every number the evaluations report rests on labels, and until spec 12b every label came
from an LLM. This audit measures the measuring instrument: for each label family it pairs
every human label with the LLM label of the *same item* and reports how far they agree.

Families and their files in `evals/labels/`:

- `spikes` (`spike_labels.jsonl`): news / not news per spike (specs 02b, 07a). A human and
  an LLM label are about the same spike when they cover the same article with
  overlapping periods; the LLM label of the same run is preferred.
- `gate` (`detection_gate_labels.jsonl`): the CI gate's frozen labels (spec 09c), paired
  with human spike labels whose period overlaps a gate burst.
- `grouping` (`grouping_labels.jsonl`): same / different story per page pair (03b).
- `relations` (`relation_labels.jsonl`): correct / wrong per extracted relation (04c).
- `qa` (`qa_judgements.jsonl`): the judges' correctness scores (0, 1, 2; linear-weighted
  kappa) and faithfulness verdicts per claim (05b).
- `qa questions` (`qa_questions.jsonl`): counted only; questions aren't judgements of an
  item two raters could disagree on.

For each pairing, per LLM labeler (model and prompt version): the counts, accuracy,
Cohen's kappa, the confusion matrix (rows human, columns LLM) and bootstrap 95%
intervals. `unsure` (a QA judgement without a score, a claim without a verdict) is the
same for both sides: such a pair is left out of accuracy and kappa, and counted.

Then the headline numbers of the committed reports in `evals/results/` are recomputed
side by side from LLM labels only and from human labels only (detection precision per
02b run, grouping precision and recall, relation precision, QA correctness and
faithfulness), each with its sample size and interval.

The audit only reads label files; it writes `evals/results/<date>_audit.md` and `.json`.
No network, no database, no LLM: it is cheap to re-run after every labeling session.
"""

import argparse
import json
import logging
import math
import random
import sys
from collections import Counter
from collections.abc import Callable, Hashable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from statistics import mean
from typing import Any

from wikipulse.config import Settings, get_settings
from wikipulse.eval.grouping import GroupingMetrics, StratumCount
from wikipulse.eval.grouping_labels import PairLabelRecord, load_pair_records
from wikipulse.eval.label_store import LabelRecord, load_records
from wikipulse.eval.metrics import percentile, sample_spikes
from wikipulse.eval.qa_judge import QAJudgement, load_judgements
from wikipulse.eval.qa_metrics import cohen_kappa
from wikipulse.eval.relation_labels import RelationLabelRecord, load_relation_records
from wikipulse.logging_setup import setup_logging

logger = logging.getLogger(__name__)

LABELS_DIR = Path("evals") / "labels"
RESULTS_DIR = Path("evals") / "results"
FILES = {
    "spikes": "spike_labels.jsonl",
    "gate": "detection_gate_labels.jsonl",
    "grouping": "grouping_labels.jsonl",
    "relations": "relation_labels.jsonl",
    "qa": "qa_judgements.jsonl",
    "qa questions": "qa_questions.jsonl",
}
UNSURE = "unsure"
# Below this many decisive pairs (neither side unsure), the report says "too few".
MIN_CONCLUSIVE = 30
DEFAULT_RESAMPLES = 2000
DEFAULT_SEED = 2026
Z95 = 1.959964
MAX_DISAGREEMENTS = 15  # listed per comparison in the report


# --- Intervals ---------------------------------------------------------------------


def wilson(successes: int, n: int) -> tuple[float, float] | None:
    """95% Wilson score interval of a proportion (sane at 0/n and n/n, unlike p ± 1.96 se)."""
    if n == 0:
        return None
    p = successes / n
    centre = (p + Z95**2 / (2 * n)) / (1 + Z95**2 / n)
    half = Z95 * math.sqrt(p * (1 - p) / n + Z95**2 / (4 * n**2)) / (1 + Z95**2 / n)
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass
class Interval:
    """A bootstrap percentile interval; `undefined` resamples had no value (e.g. kappa
    of a resample where one rater used a single category)."""

    low: float | None
    high: float | None
    undefined: int = 0

    def as_list(self) -> list[float | None]:
        return [self.low, self.high]


def bootstrap[T](
    items: Sequence[T],
    statistic: Callable[[Sequence[T]], float | None],
    resamples: int,
    seed: int,
) -> Interval:
    """95% percentile interval of `statistic` over resamples of `items` (with replacement)."""
    if not items:
        return Interval(None, None)
    rng = random.Random(seed)
    values: list[float] = []
    undefined = 0
    for _ in range(resamples):
        value = statistic([items[rng.randrange(len(items))] for _ in items])
        if value is None:
            undefined += 1
        else:
            values.append(value)
    if not values:
        return Interval(None, None, undefined)
    return Interval(percentile(values, 2.5), percentile(values, 97.5), undefined)


# --- Agreement ---------------------------------------------------------------------


@dataclass(frozen=True)
class Paired:
    """One item both a human and an LLM judged (`UNSURE` where one gave no verdict)."""

    item: str
    human: Any
    llm: Any


@dataclass
class Comparison:
    """Human labels against one LLM labeler, over one scope (a run or an evaluation)."""

    family: str
    scope: str
    labeler: str  # e.g. "ollama:qwen3:4b" or "ollama:qwen3:8b, prompt 2"
    categories: tuple[Any, ...]
    pairs: list[Paired] = field(default_factory=list)
    unpaired: int = 0  # human labels in the scope this labeler never judged
    weighted: bool = False  # linear-weighted kappa (ordered categories)
    positive: Any = None  # the category whose share each rater gives is reported


def accuracy(pairs: Sequence[Paired]) -> float | None:
    return sum(p.human == p.llm for p in pairs) / len(pairs) if pairs else None


def kappa(pairs: Sequence[Paired], categories: Sequence[Any], weighted: bool) -> float | None:
    """Cohen's kappa, or None when both sides gave one and the same category to every
    item: then chance agreement is 100% and kappa is 0/0 (`cohen_kappa` would say 1)."""
    humans, llms = [p.human for p in pairs], [p.llm for p in pairs]
    if not pairs or len(set(humans) | set(llms)) < 2:
        return None
    return cohen_kappa(humans, llms, categories, weighted=weighted)


def rare_category(pairs: Sequence[Paired], positive: Any) -> bool:
    """True when the human labels are nearly all one way (under 10% or over 90%
    positive): kappa then swings on one or two items (the "kappa paradox")."""
    share = share_of((p.human for p in pairs), positive)
    return share is not None and not 0.1 <= share <= 0.9


def confusion(pairs: Sequence[Paired], categories: Sequence[Any]) -> list[list[int]]:
    """Counts with rows = human, columns = LLM, over the categories plus `unsure`."""
    axis = [*categories, UNSURE]
    counts = Counter((p.human, p.llm) for p in pairs)
    return [[counts[(h, m)] for m in axis] for h in axis]


def conclusion(decisive: int) -> str:
    """What the sample size allows one to say."""
    if decisive == 0:
        return "no human labels yet"
    if decisive < MIN_CONCLUSIVE:
        return f"too few to conclude (n < {MIN_CONCLUSIVE})"
    return "enough for a first estimate"


def kappa_words(value: float | None) -> str:
    """Landis and Koch's rough names for kappa ranges (a convention, not a law)."""
    if value is None:
        return "undefined"
    for limit, words in ((0, "worse than chance"), (0.2, "slight"), (0.4, "fair"),
                         (0.6, "moderate"), (0.8, "substantial")):  # fmt: skip
        if value <= limit:
            return words
    return "almost perfect"


def share_of(values: Iterable[Any], positive: Any) -> float | None:
    values = list(values)
    return sum(v == positive for v in values) / len(values) if values else None


def agreement(comparison: Comparison, resamples: int, seed: int) -> dict[str, Any]:
    """Every agreement number of one comparison, JSON-ready."""
    c = comparison
    decisive = [p for p in c.pairs if UNSURE not in (p.human, p.llm)]
    acc_ci = bootstrap(decisive, accuracy, resamples, seed)
    kappa_ci = bootstrap(decisive, lambda s: kappa(s, c.categories, c.weighted), resamples, seed)
    value = kappa(decisive, c.categories, c.weighted)
    return {
        "family": c.family,
        "scope": c.scope,
        "labeler": c.labeler,
        "paired": len(c.pairs),
        "human_unsure": sum(p.human == UNSURE for p in c.pairs),
        "llm_unsure": sum(p.llm == UNSURE for p in c.pairs),
        "decisive": len(decisive),
        "unpaired_human": c.unpaired,
        "accuracy": accuracy(decisive),
        "accuracy_ci95": acc_ci.as_list(),
        "kappa": value,
        "kappa_weighted": c.weighted,
        "kappa_ci95": kappa_ci.as_list(),
        "kappa_undefined_resamples": kappa_ci.undefined,
        "kappa_words": kappa_words(value),
        "positive": c.positive,
        "human_positive_share": share_of((p.human for p in decisive), c.positive),
        "llm_positive_share": share_of((p.llm for p in decisive), c.positive),
        "categories": [*c.categories, UNSURE],
        "confusion": confusion(c.pairs, c.categories),
        "disagreements": [
            {"item": p.item, "human": p.human, "llm": p.llm} for p in decisive if p.human != p.llm
        ][:MAX_DISAGREEMENTS],
        "rare_category": rare_category(decisive, c.positive),
        "conclusion": conclusion(len(decisive)),
    }


# --- Pairing per family ------------------------------------------------------------


def latest_by[T](records: Iterable[T], key: Callable[[T], Hashable]) -> dict[Hashable, T]:
    """The last record (file order: the latest) per key."""
    found: dict[Hashable, T] = {}
    for record in records:
        found[key(record)] = record
    return found


def overlap(a: LabelRecord, b: LabelRecord) -> bool:
    """Same article and overlapping active periods: the same burst (as `Label.covers`)."""
    return a.title == b.title and a.first_edit_at <= b.end_at and b.first_edit_at <= a.end_at


def labeler_name(model: str | None, prompt_version: Any = None) -> str:
    name = model or "llm (model not recorded)"
    return name if prompt_version is None else f"{name}, prompt {prompt_version}"


def spike_match(human: LabelRecord, llm: Sequence[LabelRecord]) -> LabelRecord | None:
    """The LLM label of the human's spike: same run preferred, then the latest."""
    matching = [r for r in llm if overlap(r, human)]
    same_run = [r for r in matching if r.run_id == human.run_id]
    chosen = same_run or matching
    return chosen[-1] if chosen else None


def spike_comparisons(records: Sequence[LabelRecord]) -> list[Comparison]:
    """Human spike labels (latest per spike and run) against each LLM model's labels."""
    humans = latest_by(
        (r for r in records if r.labeler == "human"),
        lambda r: (r.run_id, r.title, r.first_edit_at),
    )
    llm = [r for r in records if r.labeler == "llm"]
    comparisons: dict[tuple[str, str], Comparison] = {}
    for model in sorted({r.model or "" for r in llm}):
        of_model = [r for r in llm if (r.model or "") == model]
        for human in humans.values():
            key = (human.run_id, model)
            if key not in comparisons:
                comparisons[key] = Comparison(
                    "spikes", human.run_id, labeler_name(model or None),
                    ("news", "not news"), positive="news",
                )  # fmt: skip
            match = spike_match(human, of_model)
            if match is None:
                comparisons[key].unpaired += 1
            else:
                item = f"{human.title} ({human.first_edit_at:%Y-%m-%d %H:%M} UTC)"
                comparisons[key].pairs.append(Paired(item, human.label, match.label))
    return [comparisons[key] for key in sorted(comparisons)]


def gate_comparisons(
    gate: Sequence[LabelRecord], spike_records: Sequence[LabelRecord]
) -> list[Comparison]:
    """The gate's LLM labels against human labels of overlapping bursts (either file)."""
    humans = [r for r in [*spike_records, *gate] if r.labeler == "human"]
    llm = [r for r in gate if r.labeler == "llm"]
    comparisons: dict[str, Comparison] = {}
    for record in llm:
        model = record.model or ""
        if model not in comparisons:
            comparisons[model] = Comparison(
                "gate", record.run_id, labeler_name(model or None),
                ("news", "not news"), positive="news",
            )  # fmt: skip
        matching = [h for h in humans if overlap(h, record)]
        if matching:
            item = f"{record.title} ({record.first_edit_at:%Y-%m-%d %H:%M} UTC)"
            comparisons[model].pairs.append(Paired(item, matching[-1].label, record.label))
    return [comparisons[key] for key in sorted(comparisons)]


def keyed_comparisons(
    family: str,
    records: Sequence[Any],
    categories: tuple[str, ...],
    positive: str,
    scope_of: Callable[[Any], str],
    item_of: Callable[[Any], str],
) -> list[Comparison]:
    """Pairing by an exact item key (`record.key`), for page pairs and relations."""
    humans = latest_by((r for r in records if r.labeler == "human"), lambda r: r.key)
    llm = [r for r in records if r.labeler == "llm"]
    comparisons: dict[tuple[str, str], Comparison] = {}
    for judge in sorted({(r.model or "", r.prompt_version or 1) for r in llm}):
        mine = latest_by(
            (r for r in llm if (r.model or "", r.prompt_version or 1) == judge), lambda r: r.key
        )
        for key, human in humans.items():
            scope = scope_of(human)
            name = labeler_name(judge[0] or None, judge[1])
            comparison = comparisons.setdefault(
                (scope, name),
                Comparison(family, scope, name, categories, positive=positive),
            )
            if key in mine:
                comparison.pairs.append(Paired(item_of(human), human.label, mine[key].label))
            else:
                comparison.unpaired += 1
    return [comparisons[key] for key in sorted(comparisons)]


def pair_item(record: PairLabelRecord) -> str:
    return f"{record.title_a} / {record.title_b}"


def relation_item(record: RelationLabelRecord) -> str:
    return f"{record.subject} {record.relation} {record.object} ({record.event_key})"


def qa_key(record: QAJudgement) -> tuple[str, str, str]:
    return (record.run, record.question_id, record.answer_hash)


def score_value(score: float | None) -> Any:
    """A correctness score as a category (0, 1, 2), or `UNSURE` without one."""
    return UNSURE if score is None else int(score)


def verdict_value(verdict: bool | None) -> Any:
    return UNSURE if verdict is None else verdict


def qa_comparisons(records: Sequence[QAJudgement]) -> list[Comparison]:
    """Human QA judgements against each judge: correctness per answer, faithfulness per
    claim (claims paired by position: both judged the same split of the answer)."""
    comparisons: dict[tuple[str, str, str], Comparison] = {}
    for metric in ("correctness", "faithfulness"):
        of_metric = [r for r in records if r.metric == metric]
        humans = latest_by((r for r in of_metric if r.labeler == "human"), qa_key)
        llm = [r for r in of_metric if r.labeler == "llm"]
        for judge in sorted({(r.model or "", r.prompt_version or "") for r in llm}):
            mine = latest_by(
                (r for r in llm if (r.model or "", r.prompt_version or "") == judge), qa_key
            )
            name = labeler_name(judge[0] or None, judge[1] or None)
            for key, human in humans.items():
                comparison = comparisons.setdefault(
                    (metric, human.run, name), qa_comparison(metric, human.run, name)
                )
                if key not in mine:
                    comparison.unpaired += 1
                elif metric == "correctness":
                    pair = Paired(human.question_id, score_value(human.score),
                                  score_value(mine[key].score))  # fmt: skip
                    comparison.pairs.append(pair)
                else:
                    comparison.pairs += claim_pairs(human, mine[key])
    return [comparisons[key] for key in sorted(comparisons)]


def qa_comparison(metric: str, run: str, name: str) -> Comparison:
    scope = f"{run} ({metric})"
    if metric == "correctness":
        return Comparison("qa", scope, name, (0, 1, 2), weighted=True, positive=2)
    return Comparison("qa", scope, name, (False, True), positive=True)


def claim_pairs(human: QAJudgement, llm: QAJudgement) -> list[Paired]:
    pairs = zip(human.claims, llm.claims, strict=False)
    return [
        Paired(
            f"{human.question_id} claim {n}", verdict_value(h.supported), verdict_value(m.supported)
        )  # fmt: skip
        for n, (h, m) in enumerate(pairs, start=1)
    ]


# --- Counts per family -------------------------------------------------------------


def family_counts(
    family: str, path: Path, records: Sequence[Any], value_of: Callable[[Any], Any],
    values: Sequence[Any],
) -> dict[str, Any]:  # fmt: skip
    """How many labels a family has, by labeler and value (zeros included)."""

    def by_value(labeler: str) -> dict[str, int]:
        counts = Counter(str(value_of(r)) for r in records if r.labeler == labeler)
        return {str(v): counts.get(str(v), 0) for v in values}

    return {
        "family": family,
        "file": path.as_posix(),
        "records": len(records),
        "human": sum(r.labeler == "human" for r in records),
        "llm": sum(r.labeler == "llm" for r in records),
        "human_by_value": by_value("human"),
        "llm_by_value": by_value("llm"),
    }


@dataclass
class LabelFiles:
    """Every label file, loaded once (read-only)."""

    spikes: list[LabelRecord]
    gate: list[LabelRecord]
    pairs: list[PairLabelRecord]
    relations: list[RelationLabelRecord]
    qa: list[QAJudgement]
    questions: list[dict[str, Any]]

    @classmethod
    def load(cls, labels_dir: Path) -> "LabelFiles":
        return cls(
            spikes=load_records(labels_dir / FILES["spikes"]),
            gate=load_records(labels_dir / FILES["gate"]),
            pairs=load_pair_records(labels_dir / FILES["grouping"]),
            relations=load_relation_records(labels_dir / FILES["relations"]),
            qa=load_judgements(labels_dir / FILES["qa"]),
            questions=load_questions(labels_dir / FILES["qa questions"]),
        )


@dataclass(frozen=True)
class QuestionRow:
    """Just enough of a QA question line to count it."""

    labeler: str
    label: str


def load_questions(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def all_counts(files: LabelFiles, labels_dir: Path) -> list[dict[str, Any]]:
    def path(family: str) -> Path:
        return labels_dir / FILES[family]

    questions = [QuestionRow(q.get("labeler", "llm"), q.get("type", "")) for q in files.questions]
    return [
        family_counts("spikes", path("spikes"), files.spikes, lambda r: r.label,
                      ("news", "not news", UNSURE)),
        family_counts("gate", path("gate"), files.gate, lambda r: r.label,
                      ("news", "not news", UNSURE)),
        family_counts("grouping", path("grouping"), files.pairs, lambda r: r.label,
                      ("same", "different", UNSURE)),
        family_counts("relations", path("relations"), files.relations, lambda r: r.label,
                      ("correct", "wrong", UNSURE)),
        family_counts("qa", path("qa"), files.qa, lambda r: r.metric,
                      ("correctness", "faithfulness")),
        family_counts("qa questions", path("qa questions"), questions, lambda r: r.label,
                      sorted({q.label for q in questions})),
    ]  # fmt: skip


def all_comparisons(files: LabelFiles) -> list[Comparison]:
    return [
        *spike_comparisons(files.spikes),
        *gate_comparisons(files.gate, files.spikes),
        *keyed_comparisons("grouping", files.pairs, ("same", "different"), "same",
                           lambda r: r.sampled_for or r.spike_run_id, pair_item),
        *keyed_comparisons("relations", files.relations, ("correct", "wrong"), "correct",
                           lambda r: r.sampled_for or "-", relation_item),
        *qa_comparisons(files.qa),
    ]  # fmt: skip


# --- Headline metrics, LLM labels next to human labels -----------------------------


def proportion(successes: int, n: int) -> dict[str, Any]:
    """A share with its sample size and 95% Wilson interval."""
    return {
        "value": successes / n if n else None,
        "n": n,
        "successes": successes,
        "ci95": list(wilson(successes, n) or (None, None)),
        "conclusion": conclusion(n),
    }


def latest_reports(results_dir: Path, prefix: str) -> list[dict[str, Any]]:
    """The latest committed report per name whose name starts with `prefix`."""
    reports: dict[str, dict[str, Any]] = {}
    for path in sorted(results_dir.glob(f"*_{prefix}*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if str(data.get("name", "")).startswith(prefix):
            data["_file"] = path.as_posix()
            reports[data["name"]] = data
    return [reports[name] for name in sorted(reports)]


@dataclass(frozen=True)
class SpikeView:
    """A spike as a 02b report lists it (`sample_spikes` reads `title`, `started_at`)."""

    title: str
    started_at: datetime
    first_edit_at: datetime
    end_at: datetime


def spike_views(report: dict[str, Any]) -> list[SpikeView]:
    return [
        SpikeView(
            s["title"],
            datetime.fromisoformat(s["started_at"]),
            datetime.fromisoformat(s["first_edit_at"]),
            datetime.fromisoformat(s["end_at"]),
        )
        for s in report["spikes"]
    ]


def label_for(spike: SpikeView, records: Sequence[LabelRecord]) -> str | None:
    """The latest label covering the spike (callers pass one labeler's records)."""
    found = [
        r.label
        for r in records
        if r.title == spike.title
        and r.first_edit_at <= spike.end_at
        and spike.first_edit_at <= r.end_at
    ]
    return found[-1] if found else None


def news_share(labels: Sequence[str | None]) -> dict[str, Any]:
    """Precision from labels: news / (news + not news); unsure and missing left out."""
    decided = [x for x in labels if x in ("news", "not news")]
    result = proportion(sum(x == "news" for x in decided), len(decided))
    result["unsure"] = sum(x == UNSURE for x in labels)
    return result


def detection_headline(report: dict[str, Any], records: Sequence[LabelRecord]) -> dict[str, Any]:
    """One 02b run: label precision from LLM labels and from human labels."""
    metrics = report["metrics"]
    sample = sample_spikes(spike_views(report), metrics["label_sample"], metrics["label_seed"])
    llm = [r for r in records if r.labeler == "llm"]
    human = [r for r in records if r.labeler == "human"]
    llm_labels = [label_for(s, llm) for s in sample]
    human_labels = [label_for(s, human) for s in sample]
    both = [
        (h, m)
        for h, m in zip(human_labels, llm_labels, strict=True)
        if h in ("news", "not news") and m
    ]
    llm_result = news_share(llm_labels)
    return {
        "run": report["name"],
        "report": report["_file"],
        "sample": len(sample),
        "reported": metrics["label_precision"],
        "llm": llm_result,
        "llm_matches_report": close(llm_result["value"], metrics["label_precision"]),
        "human": news_share(human_labels),
        "llm_on_human_sample": news_share([m for _, m in both]),
    }


def close(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) < 1e-9


def pair_start_key(a_started: datetime, b_started: datetime) -> tuple[datetime, datetime]:
    """A page pair by its two spike starts (the report names pages by their canonical
    title, the labels by the spike's title; the starts are the same in both)."""
    return (min(a_started, b_started), max(a_started, b_started))


def grouping_strata(
    report: dict[str, Any], labels: dict[tuple[datetime, datetime], str]
) -> GroupingMetrics:
    """The report's strata (populations) counted with the given labels."""
    metrics = GroupingMetrics(
        {name: StratumCount(name, s["population"]) for name, s in report["strata"].items()}
    )
    for pair in report["pairs"]:
        stratum = metrics.strata[pair["stratum"]]
        stratum.sampled += 1
        key = pair_start_key(
            datetime.fromisoformat(pair["a_started_at"]),
            datetime.fromisoformat(pair["b_started_at"]),
        )
        label = labels.get(key)
        if label is not None:
            setattr(stratum, label, getattr(stratum, label) + 1)
    return metrics


def recall_interval(
    report: dict[str, Any], labels: dict[tuple[datetime, datetime], str], resamples: int, seed: int
) -> Interval:
    """Bootstrap of the stratified recall: resample the labeled pairs within each stratum."""
    by_stratum: dict[str, list[str]] = {name: [] for name in report["strata"]}
    for pair in report["pairs"]:
        key = pair_start_key(
            datetime.fromisoformat(pair["a_started_at"]),
            datetime.fromisoformat(pair["b_started_at"]),
        )
        if labels.get(key) in ("same", "different"):
            by_stratum[pair["stratum"]].append(labels[key])
    if any(not values for values in by_stratum.values()):
        return Interval(None, None)
    rng = random.Random(seed)
    values: list[float] = []
    undefined = 0
    for _ in range(resamples):
        metrics = GroupingMetrics(
            {n: StratumCount(n, s["population"]) for n, s in report["strata"].items()}
        )
        for name, labeled in by_stratum.items():
            drawn = [labeled[rng.randrange(len(labeled))] for _ in labeled]
            metrics.strata[name].same = drawn.count("same")
            metrics.strata[name].different = drawn.count("different")
        value = metrics.recall()
        if value is None:
            undefined += 1
        else:
            values.append(value)
    if not values:
        return Interval(None, None, undefined)
    return Interval(percentile(values, 2.5), percentile(values, 97.5), undefined)


def grouping_side(
    report: dict[str, Any], labels: dict[tuple[datetime, datetime], str], resamples: int, seed: int
) -> dict[str, Any]:
    metrics = grouping_strata(report, labels)
    together = metrics.strata["together"]
    return {
        "precision": proportion(together.same, together.labeled),
        "recall": metrics.recall(),
        "recall_ci95": recall_interval(report, labels, resamples, seed).as_list(),
        "labeled_per_stratum": {n: s.labeled for n, s in metrics.strata.items()},
        "unsure": sum(s.unsure for s in metrics.strata.values()),
    }


def grouping_headline(
    report: dict[str, Any], records: Sequence[PairLabelRecord], resamples: int, seed: int
) -> dict[str, Any]:
    """One 03b run: pairwise precision and recall from LLM labels and from human labels."""

    def labels(labeler: str) -> dict[tuple[datetime, datetime], str]:
        mine = [r for r in records if r.labeler == labeler]
        return {pair_start_key(r.started_a, r.started_b): r.label for r in mine}

    llm = grouping_side(report, labels("llm"), resamples, seed)
    return {
        "run": report["name"],
        "report": report["_file"],
        "reported_precision": report["metrics"]["precision"],
        "reported_recall": report["metrics"]["recall"],
        "llm": llm,
        "llm_matches_report": close(llm["precision"]["value"], report["metrics"]["precision"]),
        "human": grouping_side(report, labels("human"), resamples, seed),
    }


def relation_headline(
    report: dict[str, Any], records: Sequence[RelationLabelRecord]
) -> dict[str, Any]:
    """One 04c run: relation precision from the report's judge and from human labels."""
    config = report["config"]["labels"]
    judge = (config["judge_model"], config["judge_prompt_version"])
    by_row = {relation_row(r): r.key for r in records}
    keys = list(
        dict.fromkeys(by_row[k] for s in report["sample"] if (k := report_row(s)) in by_row)
    )
    llm = latest_by(
        (r for r in records if r.labeler == "llm" and (r.model, r.prompt_version) == judge),
        lambda r: r.key,
    )
    human = latest_by((r for r in records if r.labeler == "human"), lambda r: r.key)

    def precision(labels: Sequence[str]) -> dict[str, Any]:
        decided = [x for x in labels if x in ("correct", "wrong")]
        result = proportion(decided.count("correct"), len(decided))
        result["unsure"] = labels.count(UNSURE)
        return result

    llm_result = precision([llm[k].label for k in keys if k in llm])
    decided = {k for k in keys if k in human and human[k].label != UNSURE}
    return {
        "run": report["name"],
        "report": report["_file"],
        "judge": labeler_name(*judge),
        "sample": len(report["sample"]),
        "matched_to_labels": len(keys),
        "reported": report["labels"]["precision"],
        "llm": llm_result,
        "llm_matches_report": close(llm_result["value"], report["labels"]["precision"]),
        "human": precision([human[k].label for k in keys if k in human]),
        "llm_on_human_sample": precision([llm[k].label for k in keys if k in decided and k in llm]),
    }


RelationRow = tuple[str, str, str, str, int | None]


def entity_text(name: str, key: str) -> str:
    """An entity as the 04c report shows it: the name, with the Wikidata id if linked."""
    return name if key.startswith("name:") else f"{name} ({key})"


def relation_row(record: RelationLabelRecord) -> RelationRow:
    return (
        record.event_key,
        entity_text(record.subject, record.subject_key),
        record.relation,
        entity_text(record.object, record.object_key),
        record.source_revision,
    )


def report_row(row: dict[str, Any]) -> RelationRow:
    """A relation of a 04c report's sample, keyed like `relation_row`."""
    return (row["event"], row["subject"], row["relation"], row["object"],
            row["source_revision"])  # fmt: skip


def mean_score(scores: Sequence[int], resamples: int, seed: int) -> dict[str, Any]:
    """Mean correctness (0 to 2) with a bootstrap interval, and the share fully correct."""
    interval = bootstrap(scores, lambda s: mean(s) if s else None, resamples, seed)
    return {
        "mean": mean(scores) if scores else None,
        "n": len(scores),
        "ci95": interval.as_list(),
        "fully_correct": proportion(scores.count(2), len(scores)),
        "conclusion": conclusion(len(scores)),
    }


def supported_share(judgements: Iterable[QAJudgement]) -> dict[str, Any]:
    """Faithfulness: supported claims / claims with a verdict, pooled."""
    verdicts = [c.supported for j in judgements for c in j.claims if c.supported is not None]
    return proportion(sum(verdicts), len(verdicts))


def qa_headline(
    report: dict[str, Any], records: Sequence[QAJudgement], resamples: int, seed: int
) -> dict[str, Any]:
    """One 05b run: correctness and faithfulness from the primary judge and from humans."""
    run, judge = report["name"], report["config"]["primary_judge"]
    mine = [r for r in records if r.run == run]

    def latest(metric: str, labeler: str) -> dict[Hashable, QAJudgement]:
        chosen = [
            r for r in mine
            if r.metric == metric and r.labeler == labeler
            and (labeler == "human" or r.model == judge)
        ]  # fmt: skip
        return latest_by(chosen, lambda r: r.question_id)

    llm, human = latest("correctness", "llm"), latest("correctness", "human")
    scores = {k: int(r.score) for k, r in llm.items() if r.score is not None}
    human_scores = {k: int(r.score) for k, r in human.items() if r.score is not None}
    llm_result = mean_score(list(scores.values()), resamples, seed)
    on_human = [scores[k] for k in human_scores if k in scores]
    return {
        "run": run,
        "report": report["_file"],
        "judge": judge,
        "reported_correctness": report["metrics"]["correctness"],
        "reported_faithfulness": report["metrics"]["faithfulness"],
        "llm_correctness": llm_result,
        "llm_matches_report": close(llm_result["mean"], report["metrics"]["correctness"]),
        "human_correctness": mean_score(list(human_scores.values()), resamples, seed),
        "llm_correctness_on_human_sample": mean_score(on_human, resamples, seed),
        "llm_faithfulness": supported_share(latest("faithfulness", "llm").values()),
        "human_faithfulness": supported_share(latest("faithfulness", "human").values()),
    }


def headlines(
    files: LabelFiles, results_dir: Path, resamples: int, seed: int
) -> dict[str, list[dict[str, Any]]]:
    return {
        "detection": [detection_headline(r, files.spikes)
                      for r in latest_reports(results_dir, "02b-")],
        "grouping": [grouping_headline(r, files.pairs, resamples, seed)
                     for r in latest_reports(results_dir, "03b-")],
        "relations": [relation_headline(r, files.relations)
                      for r in latest_reports(results_dir, "04c-")],
        "qa": [qa_headline(r, files.qa, resamples, seed)
               for r in latest_reports(results_dir, "05b-")],
    }  # fmt: skip


# --- The paid judge (optional, off by default) ----------------------------------------


def paid_judge_status(settings: Settings) -> dict[str, Any]:
    """Whether the optional paid judge may run. This audit never calls it: it only says
    why it was skipped (spec 12b allows it with ANTHROPIC_API_KEY *and* the opt-in)."""
    has_key = settings.anthropic_api_key is not None
    allowed = settings.judge_audit_allow_paid
    missing = []
    if not has_key:
        missing.append("ANTHROPIC_API_KEY")
    if not allowed:
        missing.append("JUDGE_AUDIT_ALLOW_PAID=true")
    if missing:
        reason = "skipped: " + " and ".join(missing) + " not set"
    else:
        reason = "allowed, but this audit has no paid-judge step yet (ROADMAP follow-up)"
    return {"used": False, "api_key_set": has_key, "allowed": allowed, "reason": reason}


# --- The report ----------------------------------------------------------------------


def build_report(
    labels_dir: Path,
    results_dir: Path,
    settings: Settings,
    created_at: datetime,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """The whole audit as a JSON-ready dict (reads the label files and reports only)."""
    files = LabelFiles.load(labels_dir)
    comparisons = [agreement(c, resamples, seed) for c in all_comparisons(files)]
    return {
        "created_at": created_at.isoformat(),
        "settings": {
            "resamples": resamples,
            "seed": seed,
            "min_conclusive": MIN_CONCLUSIVE,
            "interval": "95%: bootstrap percentile for accuracy, kappa, means and recall; "
            "Wilson score for single proportions",
        },
        "counts": all_counts(files, labels_dir),
        "agreement": comparisons,
        "headlines": headlines(files, results_dir, resamples, seed),
        "paid_judge": paid_judge_status(settings),
    }


def num(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.0f}%"


def ci(values: Sequence[float | None], as_pct: bool = False) -> str:
    low, high = values
    if low is None or high is None:
        return "n/a"
    show = pct if as_pct else num
    return f"[{show(low)}, {show(high)}]"


def share_text(result: dict[str, Any]) -> str:
    """`54% [40%, 67%], n = 49` (or `n/a, n = 0`)."""
    return f"{pct(result['value'])} {ci(result['ci95'], True)}, n = {result['n']}"


def render_counts(counts: Sequence[dict[str, Any]]) -> Iterable[str]:
    yield "## Label counts"
    yield ""
    yield "| Family | File | Records | Human | LLM | Human by value | LLM by value |"
    yield "|---|---|---:|---:|---:|---|---|"
    for c in counts:
        human = ", ".join(f"{k}: {v}" for k, v in c["human_by_value"].items()) or "-"
        llm = ", ".join(f"{k}: {v}" for k, v in c["llm_by_value"].items()) or "-"
        yield (
            f"| {c['family']} | `{c['file']}` | {c['records']} | {c['human']} | {c['llm']} "
            f"| {human} | {llm} |"
        )
    yield ""


def rare(row: dict[str, Any]) -> str:
    if not row["rare_category"]:
        return ""
    return "; one category is rare, so read accuracy and the matrix, not kappa"


def mismatch(row: dict[str, Any]) -> str:
    return "" if row["llm_matches_report"] else " (differs from the report!)"


def render_agreement(rows: Sequence[dict[str, Any]], counts: Sequence[dict]) -> Iterable[str]:
    yield "## Agreement of the LLM with the human labels"
    yield ""
    yield (
        "One row per scope (the run or evaluation the human labeled for) and LLM labeler. "
        "*Decisive* pairs have a verdict on both sides; pairs where either side said "
        "`unsure` are counted and left out. Intervals are bootstrap 95%. *Share* is how "
        "often each side said the first category (news, same, correct, score 2, supported)."
    )
    yield ""
    yield (
        "| Family | Scope | LLM labeler | Paired | Unsure (human / LLM) | Decisive | "
        "Accuracy [95%] | Kappa [95%] | Share human / LLM | Human unpaired | Verdict |"
    )
    yield "|---|---|---|---:|---:|---:|---|---|---|---:|---|"
    for r in rows:
        kind = "linear-weighted " if r["kappa_weighted"] else ""
        yield (
            f"| {r['family']} | {r['scope']} | {r['labeler']} | {r['paired']} | "
            f"{r['human_unsure']} / {r['llm_unsure']} | {r['decisive']} | "
            f"{pct(r['accuracy'])} {ci(r['accuracy_ci95'], True)} | "
            f"{kind}{num(r['kappa'], 2)} {ci(r['kappa_ci95'])} ({r['kappa_words']}) | "
            f"{pct(r['human_positive_share'])} / {pct(r['llm_positive_share'])} | "
            f"{r['unpaired_human']} | {r['conclusion']}{rare(r)} |"
        )
    covered = {r["family"] for r in rows if r["paired"]}
    without = [c["family"] for c in counts if c["family"] not in covered | {"qa questions"}]
    yield ""
    yield (
        "Families without any human-LLM pair: "
        + (", ".join(without) if without else "none")
        + ". Their agreement is unknown, not good or bad."
    )
    yield ""
    for r in rows:
        if not r["decisive"]:
            continue
        yield f"### {r['family']}, {r['scope']}, {r['labeler']}"
        yield ""
        yield "Rows: human. Columns: LLM."
        yield ""
        yield "| | " + " | ".join(str(c) for c in r["categories"]) + " |"
        yield "|---|" + "---:|" * len(r["categories"])
        for name, row in zip(r["categories"], r["confusion"], strict=True):
            yield f"| **{name}** | " + " | ".join(str(v) for v in row) + " |"
        if r["kappa_undefined_resamples"]:
            yield ""
            yield (
                f"{r['kappa_undefined_resamples']} resamples had no kappa (one side used a "
                "single category); the kappa interval is over the others."
            )
        if r["disagreements"]:
            yield ""
            yield "Disagreements (first few):"
            yield ""
            for d in r["disagreements"]:
                yield f"- {d['item']}: human {d['human']}, LLM {d['llm']}"
        yield ""


def render_headlines(h: dict[str, list[dict[str, Any]]]) -> Iterable[str]:
    yield "## Headline numbers: LLM labels next to human labels"
    yield ""
    yield (
        "Each number recomputed from the committed report's sample, once with LLM labels "
        "only and once with human labels only. *LLM on the human sample* uses the LLM's "
        "labels on exactly the items a human labeled, so the two can be compared directly."
    )
    yield ""
    yield "### Detection precision from labels (spec 02b)"
    yield ""
    yield "| Run | Sample | Reported | LLM labels | Human labels | LLM on the human sample |"
    yield "|---|---:|---:|---|---|---|"
    for d in h["detection"]:
        yield (
            f"| {d['run']} | {d['sample']} | {pct(d['reported'])} | {share_text(d['llm'])}"
            f"{mismatch(d)} | {share_text(d['human'])} ({d['human']['conclusion']}) | "
            f"{share_text(d['llm_on_human_sample'])} |"
        )
    yield ""
    yield "### Grouping, pairwise (spec 03b)"
    yield ""
    yield (
        "| Run | Reported precision | Precision, LLM | Precision, human | Recall, LLM | "
        "Recall, human | Human labels per stratum |"
    )
    yield "|---|---:|---|---|---|---|---|"
    for g in h["grouping"]:
        strata = ", ".join(f"{k} {v}" for k, v in g["human"]["labeled_per_stratum"].items())
        yield (
            f"| {g['run']} | {pct(g['reported_precision'])} | {share_text(g['llm']['precision'])}"
            f"{mismatch(g)} | {share_text(g['human']['precision'])} | {pct(g['llm']['recall'])} "
            f"{ci(g['llm']['recall_ci95'], True)} | {pct(g['human']['recall'])} "
            f"{ci(g['human']['recall_ci95'], True)} | {strata} |"
        )
    yield ""
    yield "### Relation precision (spec 04c)"
    yield ""
    yield (
        "| Run | Judge | Sample (found in the labels) | Reported | Judge's labels | "
        "Human labels | Judge on the human sample |"
    )
    yield "|---|---|---:|---:|---|---|---|"
    for r in h["relations"]:
        yield (
            f"| {r['run']} | {r['judge']} | {r['sample']} ({r['matched_to_labels']}) | "
            f"{pct(r['reported'])} | {share_text(r['llm'])}{mismatch(r)} | "
            f"{share_text(r['human'])} ({r['human']['conclusion']}) | "
            f"{share_text(r['llm_on_human_sample'])} |"
        )
    yield ""
    yield "### Question answering (spec 05b)"
    yield ""
    yield (
        "| Run | Judge | Correctness, judge | Correctness, human | Judge on the human "
        "sample | Faithfulness, judge | Faithfulness, human |"
    )
    yield "|---|---|---|---|---|---|---|"
    for q in h["qa"]:

        def corr(c: dict[str, Any]) -> str:
            return f"{num(c['mean'], 2)} {ci(c['ci95'])}, n = {c['n']}"

        yield (
            f"| {q['run']} | {q['judge']} | {corr(q['llm_correctness'])}{mismatch(q)} | "
            f"{corr(q['human_correctness'])} ({q['human_correctness']['conclusion']}) | "
            f"{corr(q['llm_correctness_on_human_sample'])} | "
            f"{share_text(q['llm_faithfulness'])} | {share_text(q['human_faithfulness'])} |"
        )
    yield ""


def render_markdown(report: dict[str, Any]) -> str:
    created = datetime.fromisoformat(report["created_at"])
    s = report["settings"]
    lines = [
        f"# Label audit, {created:%Y-%m-%d}",
        "",
        "LLM labelers and judges against human labels (spec 12b), from "
        "`uv run python -m wikipulse.eval.audit`. Read-only on `evals/labels/`.",
        f"Intervals: {s['interval']}; {s['resamples']} resamples, seed {s['seed']}. "
        f"Fewer than {s['min_conclusive']} decisive pairs: too few to conclude.",
        "",
        f"Paid judge: {report['paid_judge']['reason']}.",
        "",
        *render_counts(report["counts"]),
        *render_agreement(report["agreement"], report["counts"]),
        *render_headlines(report["headlines"]),
    ]
    return "\n".join(lines).rstrip() + "\n"


def write_report(report: dict[str, Any], results_dir: Path, day: date) -> tuple[Path, Path]:
    results_dir.mkdir(parents=True, exist_ok=True)
    base = results_dir / f"{day.isoformat()}_audit"
    json_path, md_path = base.with_suffix(".json"), base.with_suffix(".md")
    with json_path.open("w", encoding="utf-8", newline="\n") as out:
        out.write(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n")
    with md_path.open("w", encoding="utf-8", newline="\n") as out:
        out.write(render_markdown(report))
    return json_path, md_path


# --- Command line --------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--labels-dir", type=Path, default=LABELS_DIR)
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument(
        "--date", type=date.fromisoformat, help="the report's date (default: today, UTC)"
    )
    parser.add_argument("--resamples", type=int, default=DEFAULT_RESAMPLES)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging()
    now = datetime.now(UTC)  # wall clock: only the report's date, never in the numbers
    report = build_report(
        args.labels_dir, args.results_dir, get_settings(), now, args.resamples, args.seed
    )
    json_path, md_path = write_report(report, args.results_dir, args.date or now.date())
    logger.info("Wrote %s and %s", json_path, md_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

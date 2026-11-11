"""Drift: has the data the model meets moved away from the data it learned from?

    uv run python -m wikipulse.ml.drift --week-of 2026-11-02              # live run, that week
    uv run python -m wikipulse.ml.drift --run-id 07a-default --since 2026-11-08 --until 2026-11-10

A model is only as good as the resemblance between its training data and today's data.
An election season, a holiday or a change in the detector shifts the features (more
edits per spike, other hours, more biographies) and the model's errors grow without any
error message. Drift monitoring compares two samples with simple statistics:

- the **reference**: the training rows of the champion's dataset version (the rows it was
  fitted on, known labels), by default only those the live default detector would also
  have produced (`default_rule_fired`), because the live scorer only ever sees those;
- the **current** sample: the features and scores stored by `wikipulse.ml.serve` for one
  detector run's spikes with a cutoff in the period, scored by that same model version.

Per feature, and for the score itself:

- **PSI** (population stability index): bin the reference (deciles, or one bin per value
  for features with few values, plus one bin for "unknown"), then sum
  `(current share - reference share) * ln(current share / reference share)` over the
  bins. Rule of thumb: below 0.1 stable, 0.1 to 0.25 a moderate shift, above 0.25 a
  large one.
- **Noise floor.** That rule of thumb assumes large samples. With a few dozen spikes a
  week, PSI between two samples of the *same* distribution is often above 0.25 by
  chance alone (on average about (bins - 1) * (1/n + 1/m)). So each column gets its
  own floor (`noise_floor`): draw a fake reference and a fake current sample of the same
  sizes from the reference itself (no drift by construction), compute their PSI, repeat
  `NULL_DRAWS` times, and take the `ML_DRIFT_CONFIDENCE` quantile (0.99). A column has
  drifted when its PSI reaches both `ML_DRIFT_PSI` (big enough to matter) and the floor
  (too big to be chance). Same idea as a permutation test, with a fixed seed so a
  report is reproducible.
- **KS** (Kolmogorov-Smirnov statistic): the largest gap between the two cumulative
  distributions (0 = identical, 1 = no overlap); reported, not used for the decision.

The period has drifted when the score has, or at least `ML_DRIFT_MAX_FEATURES` features
have. With fewer than `ML_DRIFT_MIN_ROWS` scored spikes the answer is `too_few_rows`:
even with the floor, a tiny sample can only show huge shifts. Reports go to the table
`drift_reports`; a drift is logged as a warning (`DRIFT:`), and stage 9 turns that into
an alert.
"""

import argparse
import logging
import math
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

import mlflow
import numpy as np
import pandas as pd

from wikipulse.config import Settings, get_settings
from wikipulse.logging_setup import setup_logging
from wikipulse.ml.features import FEATURE_NAMES
from wikipulse.ml.models import RULE_COLUMN, TEXT_COLUMN

logger = logging.getLogger(__name__)

BINS = 10
EPSILON = 1e-4  # share given to an empty bin, so the logarithm stays finite
NULL_DRAWS = 200  # samples drawn for a column's noise floor
SCORE = "score"
REFERENCES = {
    "default-rule": "training rows the live default detector also found",
    "all": "all training rows",
}


# --- the statistics -------------------------------------------------------------------


def bin_edges(reference: np.ndarray, bins: int = BINS) -> np.ndarray:
    """Inner bin edges from the reference's known values: one bin per value when there
    are at most `bins` distinct values (flags, small counts), else deciles."""
    values = np.unique(reference)
    if len(values) <= 1:
        return values[:0]  # one bin for everything known
    if len(values) <= bins:
        return (values[:-1] + values[1:]) / 2
    return np.unique(np.quantile(reference, np.linspace(0, 1, bins + 1)[1:-1]))


def shares(values: np.ndarray, edges: np.ndarray) -> np.ndarray:
    """Share of `values` in each bin (bins from `edges`), plus a last bin for NaN."""
    known = values[~np.isnan(values)]
    counts = np.bincount(np.searchsorted(edges, known, side="right"), minlength=len(edges) + 1)
    counts = np.append(counts, np.isnan(values).sum())
    return counts / max(len(values), 1)


def psi(reference: np.ndarray, current: np.ndarray, bins: int = BINS) -> float:
    """Population stability index of `current` against `reference` (NaN = unknown)."""
    reference = np.asarray(reference, dtype=np.float64)
    current = np.asarray(current, dtype=np.float64)
    if len(reference) == 0 or len(current) == 0:
        return math.nan
    edges = bin_edges(reference[~np.isnan(reference)], bins)
    p = np.clip(shares(reference, edges), EPSILON, None)
    q = np.clip(shares(current, edges), EPSILON, None)
    return float(np.sum((q - p) * np.log(q / p)))


def noise_floor(
    reference: np.ndarray,
    n_current: int,
    confidence: float = 0.99,
    draws: int = NULL_DRAWS,
    seed: int = 0,
) -> float:
    """The PSI that chance alone stays below with probability `confidence`, for a
    current sample of `n_current` rows against this reference.

    Both samples are drawn (with replacement) from the reference, so there is no drift
    between them: whatever PSI they show is sampling noise. NaN when either is empty."""
    reference = np.asarray(reference, dtype=np.float64)
    if len(reference) == 0 or n_current == 0:
        return math.nan
    rng = np.random.default_rng(seed)
    m = len(reference)
    values = [
        psi(reference[rng.integers(0, m, m)], reference[rng.integers(0, m, n_current)])
        for _ in range(draws)
    ]
    return float(np.quantile(values, confidence))


def ks_statistic(reference: np.ndarray, current: np.ndarray) -> float:
    """Largest gap between the two empirical distribution functions (known values)."""
    a = np.sort(np.asarray(reference, dtype=np.float64))
    b = np.sort(np.asarray(current, dtype=np.float64))
    a, b = a[~np.isnan(a)], b[~np.isnan(b)]
    if len(a) == 0 or len(b) == 0:
        return math.nan
    points = np.concatenate([a, b])
    cdf_a = np.searchsorted(a, points, side="right") / len(a)
    cdf_b = np.searchsorted(b, points, side="right") / len(b)
    return float(np.max(np.abs(cdf_a - cdf_b)))


def nan_mean(values: np.ndarray) -> float:
    """The mean of the known (not NaN) values, or NaN."""
    known = values[~np.isnan(values)]
    return float(known.mean()) if len(known) else math.nan


@dataclass(frozen=True)
class ColumnDrift:
    """How one feature (or the score) compares between reference and current."""

    name: str
    psi: float
    noise_floor: float  # the PSI chance alone reaches at these sample sizes
    ks: float
    reference_mean: float
    current_mean: float
    reference_unknown: float  # share of NaN
    current_unknown: float
    drifted: bool


def column_drift(
    name: str,
    reference: np.ndarray,
    current: np.ndarray,
    threshold: float,
    confidence: float = 0.99,
    seed: int = 0,
) -> ColumnDrift:
    """PSI, noise floor and KS of one column; drifted when the PSI reaches both the
    threshold and the noise floor."""
    reference = np.asarray(reference, dtype=np.float64)
    current = np.asarray(current, dtype=np.float64)
    value = psi(reference, current)
    floor = noise_floor(reference, len(current), confidence, seed=seed)
    return ColumnDrift(
        name=name,
        psi=value,
        noise_floor=floor,
        ks=ks_statistic(reference, current),
        reference_mean=nan_mean(reference),
        current_mean=nan_mean(current),
        reference_unknown=float(np.isnan(reference).mean()) if len(reference) else math.nan,
        current_unknown=float(np.isnan(current).mean()) if len(current) else math.nan,
        drifted=not math.isnan(value) and value >= max(threshold, floor),
    )


@dataclass
class DriftReport:
    rows: int
    reference_rows: int
    status: str  # ok, drift, too_few_rows
    score: ColumnDrift
    features: list[ColumnDrift]
    news_share_reference: float
    news_share_current: float
    psi_threshold: float
    max_features: int
    confidence: float = 0.99

    @property
    def drifted_features(self) -> list[str]:
        return [f.name for f in self.features if f.drifted]

    def to_json(self) -> dict[str, Any]:
        """A JSON-safe dict (NaN -> None) for the `drift_reports` table."""
        return jsonable({**asdict(self), "drifted_features": self.drifted_features})


def jsonable(value: Any) -> Any:
    """A value with NaN replaced by None, for JSON."""
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    return value


def compare(
    reference: pd.DataFrame,
    current: pd.DataFrame,
    psi_threshold: float = 0.2,
    max_features: int = 5,
    min_rows: int = 30,
    confidence: float = 0.99,
    seed: int = 0,
) -> DriftReport:
    """Drift of `current` against `reference`: both tables hold the `FEATURE_NAMES`
    columns, `score` and `is_news`."""

    def drift_of(name: str) -> ColumnDrift:
        return column_drift(
            name,
            reference[name].to_numpy(),
            current[name].to_numpy(),
            psi_threshold,
            confidence,
            seed,
        )

    features = [drift_of(name) for name in FEATURE_NAMES]
    score = drift_of(SCORE)
    drifted = [f for f in features if f.drifted]
    if len(current) < min_rows:
        status = "too_few_rows"
    elif score.drifted or len(drifted) >= max_features:
        status = "drift"
    else:
        status = "ok"
    return DriftReport(
        rows=len(current),
        reference_rows=len(reference),
        status=status,
        score=score,
        features=sorted(features, key=lambda f: -(f.psi if not math.isnan(f.psi) else -1)),
        news_share_reference=float(reference["is_news"].mean()) if len(reference) else math.nan,
        news_share_current=float(current["is_news"].mean()) if len(current) else math.nan,
        psi_threshold=psi_threshold,
        max_features=max_features,
        confidence=confidence,
    )


# --- the two samples ------------------------------------------------------------------


def reference_table(
    settings: Settings, model: Any, which: str = "default-rule"
) -> tuple[pd.DataFrame, str]:
    """The champion's training rows (known labels) with its scores, and a description.

    `model` is a `wikipulse.ml.serve.LoadedModel`; its registry tag `dataset_version`
    says which dataset it was trained on."""
    from mlflow import MlflowClient

    from wikipulse.ml.dataset import DatasetStore
    from wikipulse.ml.train import add_summary_texts, known_rows

    tags = MlflowClient().get_model_version(model.name, model.version).tags
    version = tags.get("dataset_version")
    if not version:
        raise LookupError(f"{model.name} version {model.version} has no dataset_version tag")
    frame = DatasetStore.from_settings(settings).read(version, "train").to_pandas()
    frame, _ = known_rows(frame)
    if which == "default-rule":
        frame = frame[frame[RULE_COLUMN]].reset_index(drop=True)
    if model.needs_text:  # from the cache filled at training time
        frame = add_summary_texts(settings, {"train": frame}, offline=True)["train"]
    else:
        frame = frame.assign(**{TEXT_COLUMN: frame["title"]})
    out = model.predict(frame)
    table = frame[FEATURE_NAMES].assign(
        score=out["score"].to_numpy(), is_news=out["is_news"].to_numpy()
    )
    return table, f"{version} train, {REFERENCES[which]}"


def current_table(scored: list[Any]) -> pd.DataFrame:
    """The stored scores (`wikipulse.db.scores.ScoredSpike`) as a table like the
    reference: features (null -> NaN), `score`, `is_news`."""
    rows = [
        {
            **{
                name: np.nan if s.features.get(name) is None else s.features[name]
                for name in FEATURE_NAMES
            },
            "score": s.score,
            "is_news": s.is_news,
        }
        for s in scored
    ]
    return pd.DataFrame(rows, columns=[*FEATURE_NAMES, "score", "is_news"]).astype(
        {name: "float64" for name in [*FEATURE_NAMES, "score"]}
    )


def run_drift(
    conn: Any,
    settings: Settings,
    model: Any,
    run_id: str,
    since: datetime,
    until: datetime,
    which: str = "default-rule",
    store: bool = True,
) -> DriftReport:
    """Compare one run's scored spikes in [since, until) with the training data, and
    store the report (replacing an earlier one for the same model, run and period)."""
    from wikipulse.db.scores import save_drift_report, score_versions, scored_spikes

    scored = scored_spikes(conn, run_id, model.name, model.version, since, until)
    others = {
        k: v
        for k, v in score_versions(conn, run_id, since, until).items()
        if k != f"{model.name}/{model.version}"
    }
    if others:
        logger.info("Left out, scored by other versions: %s", others)
    reference, description = reference_table(settings, model, which)
    report = compare(
        reference,
        current_table(scored),
        settings.ml_drift_psi,
        settings.ml_drift_max_features,
        settings.ml_drift_min_rows,
        settings.ml_drift_confidence,
        settings.ml_seed,
    )
    if report.status == "drift":
        logger.warning(
            "DRIFT: %s v%s on run %s, %s to %s: score PSI %.3f, %d features drifted (%s)",
            model.name,
            model.version,
            run_id,
            since.date(),
            until.date(),
            report.score.psi,
            len(report.drifted_features),
            ", ".join(report.drifted_features),
        )
    if store:
        save_drift_report(
            conn,
            model.name,
            model.version,
            run_id,
            (since, until),
            description,
            report.rows,
            report.status,
            report.to_json(),
        )
    return report


def render(report: DriftReport, top: int = 12) -> str:
    """A drift report as text, the `top` most drifted features first."""

    def fmt(value: float) -> str:
        return "   n/a" if math.isnan(value) else f"{value:6.3f}"

    lines = [
        f"Status: {report.status}  ({report.rows} scored spikes vs. {report.reference_rows} "
        f"training rows; PSI threshold {report.psi_threshold:g}, or the noise floor "
        f"at {report.confidence:.0%} if higher)",
        f"News share: {report.news_share_current:.1%} now vs. {report.news_share_reference:.1%} "
        "in training (at the model's threshold)",
        "",
        "column                    PSI  floor     KS    mean ref   mean now  unknown ref/now",
    ]
    for c in [report.score, *report.features[:top]]:
        lines.append(
            f"{c.name:<24} {fmt(c.psi)} {fmt(c.noise_floor)} {fmt(c.ks)} "
            f"{c.reference_mean:10.3f} {c.current_mean:10.3f}"
            f"  {c.reference_unknown:.0%}/{c.current_unknown:.0%}{'  DRIFT' if c.drifted else ''}"
        )
    return "\n".join(lines)


# --- the command ----------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    from wikipulse.ml.dataset import parse_moment

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", help="detector run (default ML_SERVE_RUN_ID)")
    parser.add_argument("--week-of", type=parse_moment, help="period: this day and the 6 after")
    parser.add_argument("--since", type=parse_moment, help="period start (UTC, spike cutoffs)")
    parser.add_argument("--until", type=parse_moment, help="period end (exclusive)")
    parser.add_argument("--version", help="model version (default: the champion)")
    parser.add_argument("--reference", choices=sorted(REFERENCES), default="default-rule")
    parser.add_argument("--no-store", action="store_true", help="print only")
    parser.add_argument("--model-name", help="registered model (default ML_MODEL_NAME)")
    args = parser.parse_args(argv)
    if args.week_of:
        args.since, args.until = args.week_of, args.week_of + timedelta(days=7)
    if not (args.since and args.until):
        parser.error("give --week-of, or --since and --until")
    return args


def main(argv: list[str] | None = None) -> int:
    import psycopg

    from wikipulse.ml.serve import load_alias, load_version

    args = parse_args(argv)
    settings = get_settings()
    setup_logging(settings.log_level)
    mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    name = args.model_name or settings.ml_model_name
    model = load_version(name, args.version) if args.version else load_alias(name)
    run_id = args.run_id or settings.ml_serve_run_id
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as conn:
        report = run_drift(
            conn,
            settings,
            model,
            run_id,
            args.since,
            args.until,
            args.reference,
            store=not args.no_store,
        )
    print(f"{name} v{model.version}, run {run_id}, {args.since:%Y-%m-%d} to {args.until:%Y-%m-%d}")
    print(render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())

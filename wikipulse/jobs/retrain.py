"""Weekly retraining of the news classifier: the steps of `dags/retrain.py`.

    python -m wikipulse.jobs.retrain drift    --date 2026-11-02 [--run-id live]
    python -m wikipulse.jobs.retrain extend   --date 2026-11-02 [--lake-prefix history/edits]
    python -m wikipulse.jobs.retrain build    --date 2026-11-02 [--lake-prefix history/edits]
    python -m wikipulse.jobs.retrain train    --date 2026-11-02 --dataset v3
    python -m wikipulse.jobs.retrain evaluate --date 2026-11-02 --dataset v3 --challenger 4
    python -m wikipulse.jobs.retrain promote  --date 2026-11-02 --challenger 4

`--date` is the start of the week the run is for (the DAG's data interval).

1. **drift**: the week's scored spikes against the champion's training data
   (`wikipulse.ml.drift`), stored in `drift_reports`. Drift is reported, not a failure.
2. **extend**: brings the history lake (`ML_LAKE_PREFIX`, `history/edits`) up to the
   last complete hour, by reading the stream's retained history from the lake's last
   hour on (`wikipulse.lake.backfill`; one connection, polite, idempotent). Without it
   every week would rebuild the same dataset from the same edits. Skipped for the live
   archive (`LAKE_PREFIX`), which the archiver extends itself.
3. **build**: a new dataset version from the lake (`wikipulse.ml.dataset build`). The last
   line printed is its name, which the DAG hands to the next steps.
4. **train**: the four models of spec 07b on it (`ML_RETRAIN_MODELS`), chosen by the
   same fixed rule (`ML_SELECT_RULE`) but on the *validation* split, so the test split
   is not used to pick the challenger and stays a fair ground for step 4. The chosen one
   is registered as a new version with the alias `@challenger` and `status=challenger`;
   older versions are *not* marked superseded. Running the step again for the same week
   and dataset reuses that version.
5. **evaluate**: champion and challenger score the *same* rows: the labeled test split of
   the new dataset. The decision (`decide`): promote only if the challenger's test
   average precision beats the champion's by at least `ML_PROMOTE_MARGIN`, with at least
   `ML_PROMOTE_MIN_TEST_ROWS` labeled rows. Everything is logged as an MLflow run
   (`role=promotion`) and as tags on the challenger version.
6. **promote**: carries the decision out: `@champion` moves to the challenger
   (`wikipulse.ml.registry.promote`), or the challenger is tagged `status=rejected`. It
   refuses if the champion changed after the evaluation (someone promoted or rolled back
   by hand in between): then the comparison is out of date.

Every step can run again safely. Once the week's challenger has been promoted (or has
served at all, `wikipulse.ml.registry.past_challenger`), train passes its number on
without touching it, and evaluate and promote print `skipped`: the champion is never
made a challenger again, re-evaluated, or tagged rejected.
"""

import argparse
import asyncio
import json
import logging
import re
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from wikipulse.config import Settings, get_settings
from wikipulse.logging_setup import setup_logging

logger = logging.getLogger(__name__)

STEPS = ("drift", "extend", "build", "train", "evaluate", "promote")
FORMATS = {"dataset": re.compile(r"v\d+"), "challenger": re.compile(r"\d+")}
PROMOTE, REJECT = "promote", "reject"
# The stream keeps about two weeks of history; the extend step stays well inside that.
HISTORY_RETENTION = timedelta(days=12)
# An hour is complete once the stream is a few minutes past its end (late events).
HOUR_SETTLE = timedelta(minutes=10)
SKIPPED = "skipped"  # a repeated step on a version that is past the challenger stage


# --- the decision (pure, unit-tested) -------------------------------------------------


@dataclass
class Comparison:
    """Champion and challenger on the same labeled test rows."""

    dataset: str
    rows: int
    positives: int
    champion_version: str | None
    challenger_version: str
    champion_ap: float
    challenger_ap: float
    gap: float  # challenger - champion
    gap_low: float  # 95 % paired-bootstrap interval of the gap
    gap_high: float
    champion_precision: float
    champion_recall: float
    challenger_precision: float
    challenger_recall: float
    champion_trained_on_test_rows: int = 0


def decide(c: Comparison, margin: float, min_rows: int) -> tuple[str, str]:
    """Promote the challenger, or reject it, with the reason.

    The rule is fixed in config before the comparison is made: the challenger must beat
    the champion's test average precision by at least `margin` on the same rows, and
    there must be at least `min_rows` labeled rows of both classes. A tie, or a gain
    smaller than the margin, keeps the champion: a new model is a risk (new behaviour,
    new bugs) that only a real improvement pays for."""
    if c.champion_version is None:
        return PROMOTE, "there is no champion yet"
    if c.challenger_version == c.champion_version:
        return REJECT, "the challenger is the champion itself"
    if c.rows < min_rows:
        return (
            REJECT,
            f"only {c.rows} labeled test rows (fewer than {min_rows}): too few to compare",
        )
    if c.positives in (0, c.rows):
        return REJECT, "the test rows hold only one class: average precision can't compare"
    verdict = (
        f"test average precision {c.challenger_ap:.3f} (challenger v{c.challenger_version}) vs. "
        f"{c.champion_ap:.3f} (champion v{c.champion_version}) on the same {c.rows} rows of "
        f"{c.dataset}: gap {c.gap:+.3f} (95% {c.gap_low:+.3f} to {c.gap_high:+.3f})"
    )
    if c.champion_trained_on_test_rows:
        verdict += (
            f"; the champion was trained on {c.champion_trained_on_test_rows} of these rows, "
            "so its number is optimistic"
        )
    if c.gap >= margin:
        return PROMOTE, f"{verdict}, at least the margin {margin:g}"
    return REJECT, f"{verdict}, below the margin {margin:g}"


# --- the steps ------------------------------------------------------------------------


def retrain_key(day: date) -> str:
    """The name of a retraining cycle (its day)."""
    return f"retrain-{day.isoformat()}"


def parse_models(text: str) -> tuple[str, ...]:
    """The models to train from `ML_RETRAIN_MODELS` ("logreg, gbt"), in `MODELS` order.

    Spaces around names are ignored; an unknown name or an empty list is an error,
    rather than silently training fewer models (or none)."""
    from wikipulse.ml.train import MODELS

    names = {n.strip() for n in text.split(",") if n.strip()}
    unknown = names - set(MODELS)
    if unknown:
        raise ValueError(
            f"ML_RETRAIN_MODELS: unknown model(s) {sorted(unknown)}; choose from {MODELS}"
        )
    if not names:
        raise ValueError(f"ML_RETRAIN_MODELS is empty; choose from {MODELS}")
    return tuple(m for m in MODELS if m in names)


def step_drift(settings: Settings, day: date, run_id: str | None) -> int:
    """The `check_drift` task: the champion's drift report; returns the exit code."""
    import psycopg

    from wikipulse.ml.drift import render, run_drift
    from wikipulse.ml.serve import load_alias

    model = load_alias(settings.ml_model_name)
    run_id = run_id or settings.ml_serve_run_id
    since = datetime(day.year, day.month, day.day, tzinfo=UTC)
    with psycopg.connect(settings.postgres_dsn, autocommit=True, connect_timeout=10) as conn:
        report = run_drift(conn, settings, model, run_id, since, since + timedelta(days=7))
    logger.info(
        "Drift of v%s on run %s, week of %s:\n%s", model.version, run_id, day, render(report)
    )
    return 0


def floor_hour(moment: datetime) -> datetime:
    """`moment` rounded down to the hour."""
    return moment.replace(minute=0, second=0, microsecond=0)


def extend_window(last_hour: datetime | None, now: datetime) -> tuple[datetime, datetime] | None:
    """The hours [since, until) the extend step backfills, or None if the lake is current.

    From the hour after the lake's last one to the last complete hour. The stream only
    keeps recent history: an older start is moved up to `HISTORY_RETENTION` ago (the
    lake then has a gap, and the dataset build uses the stretch after it)."""
    until = floor_hour(now - HOUR_SETTLE)
    earliest = floor_hour(now - HISTORY_RETENTION) + timedelta(hours=1)
    since = last_hour + timedelta(hours=1) if last_hour else earliest
    if since < earliest:
        logger.warning(
            "The lake ends at %s, older than the stream's history: it will have a gap "
            "from %s to %s",
            last_hour,
            since,
            earliest,
        )
        since = earliest
    return (since, until) if since < until else None


def step_extend(settings: Settings, lake_prefix: str | None) -> int:
    """The `extend_history` task: backfill the history lake; returns the exit code."""
    from wikipulse.lake import backfill
    from wikipulse.lake.lock import LockLostError
    from wikipulse.lake.store import LakeStore
    from wikipulse.stream.consume import warn_if_placeholder_user_agent

    prefix = (lake_prefix or settings.ml_lake_prefix).strip("/")
    if prefix == settings.lake_prefix.strip("/"):
        logger.info("%s is the live archive: the archiver extends it", prefix)
        return 0
    hours = LakeStore.from_settings(settings, prefix).hours()
    window = extend_window(hours[-1] if hours else None, datetime.now(UTC))
    if window is None:
        logger.info("%s is up to date (last hour %s)", prefix, hours[-1] if hours else None)
        return 0
    since, until = window
    warn_if_placeholder_user_agent(settings)
    args = argparse.Namespace(since=since.isoformat(), until=until.isoformat(), prefix=prefix)
    try:
        done = asyncio.run(backfill.run(args, settings))
    except LockLostError as error:  # another writer: the task's retry tries again
        logger.error("%s", error)
        return 1
    logger.info("Extended %s from %s to %s: %d edits", prefix, since, until, done.kept)
    return 0


def step_build(settings: Settings, lake_prefix: str | None) -> int:
    """The `build_dataset` task: build a dataset version; returns the exit code."""
    from wikipulse.ml.dataset import BuildOptions, build

    logging.getLogger("wikipulse.detect").setLevel(logging.WARNING)  # a line per spike
    version = build(BuildOptions(lake_prefix=lake_prefix or None), settings)
    print(version, flush=True)  # the last line: the DAG passes it on (XCom)
    return 0


def existing_challenger(client: Any, name: str, key: str, dataset: str) -> str | None:
    """A version registered earlier by this week's run for this dataset (a retried task)."""
    for v in client.search_model_versions(f"name = '{name}'"):
        if v.tags.get("retrain_key") == key and v.tags.get("dataset_version") == dataset:
            return str(v.version)
    return None


def step_train(settings: Settings, day: date, dataset: str) -> int:
    """The `train_challenger` task: train and register a challenger."""
    import mlflow

    from wikipulse.events.embed import Embedder
    from wikipulse.ml.models import TEXT_COLUMN
    from wikipulse.ml.registry import (
        CHALLENGER,
        STATUS_CHALLENGER,
        past_challenger,
        set_status,
    )
    from wikipulse.ml.train import (
        TrainConfig,
        add_summary_texts,
        load_dataset,
        train_all,
    )

    client = mlflow.MlflowClient()
    name, key = settings.ml_model_name, retrain_key(day)
    version = existing_challenger(client, name, key, dataset)
    if version is not None and (why := past_challenger(client, name, version)):
        # This week's run already ended in a promotion: never make the champion (or a
        # version that served) a challenger again.
        logger.info(
            "Version %s was trained for %s on %s, and %s: leaving it", version, key, dataset, why
        )
        print(version, flush=True)
        return 0
    if version is None:
        models = parse_models(settings.ml_retrain_models)
        frames, info = load_dataset(settings, dataset)
        embedder = None
        if "torch" in models:
            frames = add_summary_texts(settings, frames, offline=False)
            embedder = Embedder(settings.events_embed_model)
        else:
            frames = {k: f.assign(**{TEXT_COLUMN: f["title"]}) for k, f in frames.items()}
        config = TrainConfig(
            seed=settings.ml_seed,
            target_precision=settings.ml_target_precision,
            select_rule=settings.ml_select_rule,
            select_margin=settings.ml_select_margin,
            select_split="valid",  # keep the test split for the comparison with the champion
            embed_model=settings.events_embed_model,
            models=models,
        )
        report = train_all(
            frames,
            info,
            embedder,
            config,
            settings.ml_experiment,
            name,
            supersede=False,
            extra_tags={"retrain_key": key, "trigger": "weekly retraining"},
        )
        version = report.registered_version
        logger.info("Challenger: %s (%s), %s", report.chosen, version, report.reason)
    else:
        logger.info(
            "Version %s was already trained for %s on %s; reusing it", version, key, dataset
        )
    assert version is not None
    client.set_registered_model_alias(name, CHALLENGER, version)
    set_status(client, name, version, STATUS_CHALLENGER)
    print(version, flush=True)  # the last line: the DAG passes it on (XCom)
    return 0


def threshold_pr(y: np.ndarray, scores: np.ndarray, threshold: float) -> tuple[float, float]:
    """Precision and recall of `scores` at `threshold`."""
    from wikipulse.ml.evaluate import threshold_metrics

    m = threshold_metrics(y, scores, threshold)
    return m["precision"], m["recall"]


def compare_models(
    champion: Any, challenger: Any, frame: Any, dataset: str, seed: int, seen: int = 0
) -> Comparison:
    """Score the same labeled rows with both models (`LoadedModel`s; champion may be
    None) and compare them."""
    from sklearn.metrics import average_precision_score

    from wikipulse.ml.evaluate import paired_gap_interval

    y = frame["label"].to_numpy(dtype=np.int64)
    b = challenger.predict(frame)["score"].to_numpy(dtype=np.float64)
    b_precision, b_recall = threshold_pr(y, b, challenger.threshold)
    two_classes = len(np.unique(y)) == 2
    b_ap = float(average_precision_score(y, b)) if two_classes else np.nan
    if champion is None:
        a_ap = gap = low = high = a_precision = a_recall = np.nan
    else:
        a = champion.predict(frame)["score"].to_numpy(dtype=np.float64)
        a_precision, a_recall = threshold_pr(y, a, champion.threshold)
        if two_classes:
            gap, low, high = paired_gap_interval(y, b, a, seed)  # AP(challenger) - AP(champion)
            a_ap = float(average_precision_score(y, a))
        else:
            a_ap = gap = low = high = np.nan
    return Comparison(
        dataset=dataset,
        rows=len(y),
        positives=int(y.sum()),
        champion_version=champion.version if champion else None,
        challenger_version=challenger.version,
        champion_ap=a_ap,
        challenger_ap=b_ap,
        gap=gap,
        gap_low=low,
        gap_high=high,
        champion_precision=a_precision,
        champion_recall=a_recall,
        challenger_precision=b_precision,
        challenger_recall=b_recall,
        champion_trained_on_test_rows=seen,
    )


def rows_seen_by(settings: Settings, model: Any, spike_ids: set[str]) -> int:
    """How many of these spikes were in the train or valid split the model learned from
    (its dataset version's)."""
    import mlflow

    from wikipulse.ml.dataset import DatasetStore

    version = (
        mlflow.MlflowClient()
        .get_model_version(model.name, model.version)
        .tags.get("dataset_version")
    )
    if not version:
        return 0
    store = DatasetStore.from_settings(settings)
    seen = set()
    for split in ("train", "valid"):
        seen |= set(store.read(version, split).column("spike_id").to_pylist())
    return len(seen & spike_ids)


def log_comparison(settings: Settings, c: Comparison, decision: str, reason: str, key: str) -> str:
    """The comparison as an MLflow run (role=promotion); returns its id."""
    import mlflow

    from wikipulse.ml.evaluate import finite
    from wikipulse.ml.train import ensure_experiment

    mlflow.set_experiment(experiment_id=ensure_experiment(settings.ml_experiment))
    champion = f"v{c.champion_version}" if c.champion_version else "none"
    name = f"champion {champion} vs. challenger v{c.challenger_version}"
    with mlflow.start_run(run_name=name) as run, tempfile.TemporaryDirectory() as tmp:
        mlflow.set_tags(
            {
                "role": "promotion",
                "dataset_version": c.dataset,
                "champion_version": c.champion_version or "none",
                "challenger_version": c.challenger_version,
                "decision": decision,
                "decision_reason": reason,
                "retrain_key": key,
            }
        )
        mlflow.log_params(
            {
                "promote_margin": settings.ml_promote_margin,
                "promote_min_test_rows": settings.ml_promote_min_test_rows,
            }
        )
        numbers = {k: float(v) for k, v in asdict(c).items() if isinstance(v, int | float)}
        mlflow.log_metrics(finite(numbers))
        path = Path(tmp) / "decision.json"
        record = {**asdict(c), "decision": decision, "reason": reason}
        path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8", newline="\n")
        mlflow.log_artifact(str(path))
        return run.info.run_id


def step_evaluate(settings: Settings, day: date, dataset: str, challenger_version: str) -> int:
    """The `evaluate_against_champion` task: compare on the same test rows."""
    import mlflow

    from wikipulse.ml.models import TEXT_COLUMN
    from wikipulse.ml.registry import CHAMPION, alias_version, past_challenger
    from wikipulse.ml.serve import load_version
    from wikipulse.ml.train import add_summary_texts, known_rows, load_dataset

    client = mlflow.MlflowClient()
    name = settings.ml_model_name
    if why := past_challenger(client, name, challenger_version):
        # A repeated run after the promotion: the decision made then stays on the version.
        logger.info("Nothing to evaluate: %s", why)
        print(SKIPPED, flush=True)
        return 0
    champion_version = alias_version(client, name, CHAMPION)
    challenger = load_version(name, challenger_version)
    champion = load_version(name, champion_version) if champion_version else None
    frames, _ = load_dataset(settings, dataset)
    test, _ = known_rows(frames["test"])
    if challenger.needs_text or (champion is not None and champion.needs_text):
        test = add_summary_texts(settings, {"test": test}, offline=False)["test"]
    else:
        test = test.assign(**{TEXT_COLUMN: test["title"]})
    seen = rows_seen_by(settings, champion, set(test["spike_id"])) if champion else 0
    comparison = compare_models(champion, challenger, test, dataset, settings.ml_seed, seen)
    decision, reason = decide(
        comparison, settings.ml_promote_margin, settings.ml_promote_min_test_rows
    )
    key = retrain_key(day)
    run_id = log_comparison(settings, comparison, decision, reason, key)
    for tag, value in {
        "decision": decision,
        "decision_reason": reason,
        "evaluation_run": run_id,
        "evaluated_on": dataset,
        "champion_at_evaluation": champion_version or "none",
    }.items():
        client.set_model_version_tag(name, challenger_version, tag, value)
    logger.info("Decision for version %s: %s (%s)", challenger_version, decision, reason)
    print(decision, flush=True)
    return 0


def step_promote(settings: Settings, challenger_version: str) -> int:
    """The `promote_or_reject` task: act on the evaluation's decision."""
    import mlflow

    from wikipulse.ml.registry import (
        CHAMPION,
        STATUS_REJECTED,
        alias_version,
        describe,
        past_challenger,
        promote,
        set_status,
    )

    client = mlflow.MlflowClient()
    name = settings.ml_model_name
    if why := past_challenger(client, name, challenger_version):
        # Already promoted (by this run's first attempt, or by hand): never tag it
        # rejected, never promote it a second time.
        logger.info("Nothing to promote or reject: %s", why)
        print(SKIPPED, flush=True)
        return 0
    tags = client.get_model_version(name, challenger_version).tags
    decision = tags.get("decision")
    if decision not in (PROMOTE, REJECT):
        logger.error("Version %s has no decision yet: run the evaluate step", challenger_version)
        return 1
    reason = tags.get("decision_reason", "")
    if decision == PROMOTE:
        current = alias_version(client, name, CHAMPION) or "none"
        if current != tags.get("champion_at_evaluation"):
            logger.error(
                "The champion changed since the evaluation (%s then, %s now): evaluate again",
                tags.get("champion_at_evaluation"),
                current,
            )
            return 1
        change = promote(client, name, challenger_version, reason)
        logger.info("PROMOTED: champion %s -> %s", change.before, change.after)
    else:
        set_status(client, name, challenger_version, STATUS_REJECTED, rejected_reason=reason)
        logger.info("REJECTED: version %s stays a challenger only; %s", challenger_version, reason)
    logger.info("Registry now:\n%s", describe(client, name))
    print(decision, flush=True)
    return 0


# --- the command ----------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("step", choices=STEPS)
    parser.add_argument("--date", required=True, type=date.fromisoformat, help="week start (UTC)")
    parser.add_argument(
        "--run-id", default="", help="drift: detector run (default ML_SERVE_RUN_ID)"
    )
    parser.add_argument(
        "--lake-prefix", default="", help="extend/build: lake prefix (ML_LAKE_PREFIX)"
    )
    parser.add_argument("--dataset", default="", help="train/evaluate: dataset version, e.g. v3")
    parser.add_argument("--challenger", default="", help="evaluate/promote: model version")
    args = parser.parse_args(argv)
    needs = {"train": ["dataset"], "evaluate": ["dataset", "challenger"], "promote": ["challenger"]}
    for option in needs.get(args.step, []):
        value = getattr(args, option).strip()
        if not value:
            parser.error(f"{args.step} needs --{option}")
        if not FORMATS[option].fullmatch(value):  # they arrive from another task's output
            parser.error(f"--{option} {value!r} doesn't look like {FORMATS[option].pattern}")
    return args


def main(argv: list[str] | None = None) -> int:
    import mlflow

    args = parse_args(argv)
    # MLflow prints run links with an emoji; a redirected stdout (cp1252) can't encode it.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    settings = get_settings()
    setup_logging(settings.log_level)
    mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    if args.step == "drift":
        return step_drift(settings, args.date, args.run_id.strip() or None)
    if args.step == "extend":
        return step_extend(settings, args.lake_prefix.strip() or None)
    if args.step == "build":
        return step_build(settings, args.lake_prefix.strip() or None)
    if args.step == "train":
        return step_train(settings, args.date, args.dataset.strip())
    if args.step == "evaluate":
        return step_evaluate(settings, args.date, args.dataset.strip(), args.challenger.strip())
    return step_promote(settings, args.challenger.strip())


if __name__ == "__main__":
    sys.exit(main())

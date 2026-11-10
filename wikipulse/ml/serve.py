"""Score spikes with the champion news classifier: `uv run python -m wikipulse.ml.serve`.

    uv run python -m wikipulse.ml.serve                     # live: STREAM_KEY, run ML_SERVE_RUN_ID
    uv run python -m wikipulse.ml.serve live --stream-key wiki:replay:edits --run-id myrun
    uv run python -m wikipulse.ml.serve backfill --run-id 07a-default   # past spikes, from the lake

**What is served.** `models:/<ML_MODEL_NAME>@champion`, whichever version the alias
points at (`wikipulse.ml.registry`). The scorer pins the version it loaded, writes it
next to every score, and checks the alias every `ML_SERVE_RELOAD_S` seconds: after a
promotion or a rollback, the next spikes are scored by the new champion, without a
restart.

**When a spike is scored.** Once, at its cutoff: `FEATURE_WINDOW_MIN` (10) minutes after
it started, with exactly the features the model was trained on (`features.py`): the
page's edits up to the cutoff, its 24 hours of history, and the page's revision at the
cutoff (MediaWiki API, cached). "At its cutoff" is *event time*: the live scorer scores a
spike when the edit stream's newest event time has passed the cutoff by
`ML_SERVE_LATENESS_S`. So a replayed recording gets the same scores as the live run.

**Where the edits come from.**
- `live`: the scorer reads the Redis edit stream itself (no consumer group, it only
  reads) and keeps the last `ML_SERVE_BUFFER_HOURS` of edits per page in memory
  (`EditBuffer`). At start it reads that much of the stream back, so a restart loses no
  history.
- `backfill`: the lake (`--lake-prefix`, default `ML_LAKE_PREFIX`), for spikes of past
  runs, e.g. the default detector's spikes over the archived history.

**Where scores go.** Table `spike_scores` (one row per spike: model version, score,
`is_news`, the features); the event feed and the web page show them.
"""

import argparse
import logging
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

import mlflow
import pandas as pd
from mlflow import MlflowClient

from wikipulse.config import Settings, get_settings
from wikipulse.db.scores import SpikeScore, SpikeToScore, spikes_to_score, upsert_scores
from wikipulse.logging_setup import setup_logging
from wikipulse.ml.dataset import revision_at
from wikipulse.ml.features import HISTORY, PageEdit, PageSnapshot, SpikeWindow, check_no_leakage
from wikipulse.ml.models import INPUT_COLUMNS, RULE_COLUMN, TEXT_COLUMN
from wikipulse.ml.registry import CHAMPION, alias_version
from wikipulse.ml.summaries import Lead, summary_text
from wikipulse.stream.models import EditEvent

logger = logging.getLogger(__name__)


# --- the model ------------------------------------------------------------------------


@dataclass
class LoadedModel:
    """One registered model version, loaded and ready to score."""

    name: str
    version: str
    model: Any  # mlflow.pyfunc.PyFuncModel
    threshold: float
    needs_text: bool  # does it read `summary_text` (the neural network)?
    model_type: str = "?"

    def predict(self, frame: pd.DataFrame) -> pd.DataFrame:
        """`score` and `is_news` for each row of a table with `INPUT_COLUMNS`."""
        return self.model.predict(frame[INPUT_COLUMNS])


def load_version(name: str, version: str) -> LoadedModel:
    """Load `models:/<name>/<version>` (a pinned version, not a moving alias)."""
    model = mlflow.pyfunc.load_model(f"models:/{name}/{version}")
    inner = model.unwrap_python_model()  # wikipulse.ml.models.NewsClassifierModel
    scorer = getattr(inner, "scorer", None)
    tags = MlflowClient().get_model_version(name, version).tags
    return LoadedModel(
        name=name,
        version=str(version),
        model=model,
        threshold=float(inner.threshold),
        needs_text=bool(getattr(scorer, "needs_embeddings", False)),
        model_type=tags.get("model_type", "?"),
    )


def load_alias(name: str, alias: str = CHAMPION) -> LoadedModel:
    """Load the version an alias points at (`models:/<name>@<alias>`)."""
    version = alias_version(MlflowClient(), name, alias)
    if version is None:
        raise LookupError(
            f"{name} has no @{alias}; set one with: "
            f"uv run python -m wikipulse.ml.registry promote <version>"
        )
    return load_version(name, version)


class ModelHolder:
    """The champion, reloaded when its alias moves (checked every `reload_s` seconds).

    The check uses the wall clock on purpose: it is about operations (how quickly a
    promotion takes effect), not about which data a score uses. If the registry can't be
    reached, the model already loaded keeps serving.
    """

    def __init__(
        self,
        name: str,
        alias: str = CHAMPION,
        reload_s: float = 300,
        resolve: Callable[[str, str], str | None] | None = None,
        load: Callable[[str, str], LoadedModel] = load_version,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.alias = alias
        self.reload_s = reload_s
        self.resolve = resolve or (lambda n, a: alias_version(MlflowClient(), n, a))
        self.load = load
        self.clock = clock
        self.model: LoadedModel | None = None
        self._checked = 0.0

    def current(self) -> LoadedModel:
        now = self.clock()
        if self.model is not None and now - self._checked < self.reload_s:
            return self.model
        self._checked = now
        try:
            version = self.resolve(self.name, self.alias)
        except Exception as error:  # any registry outage
            if self.model is None:
                raise
            logger.warning(
                "Registry not reachable, keeping version %s: %s", self.model.version, error
            )
            return self.model
        if version is None:
            if self.model is None:
                raise LookupError(f"{self.name} has no @{self.alias}")
            logger.warning("@%s is not set; keeping version %s", self.alias, self.model.version)
            return self.model
        if self.model is None or self.model.version != version:
            before = self.model.version if self.model else None
            self.model = self.load(self.name, version)
            logger.info(
                "Serving %s version %s (%s, threshold %.3f)%s",
                self.name,
                version,
                self.model.model_type,
                self.model.threshold,
                f", was version {before}" if before else "",
            )
        return self.model


# --- the page at the cutoff -----------------------------------------------------------


class PageSource(Protocol):
    """Old revisions of pages: their parsed snapshot, and their lead (for text models)."""

    def snapshots(self, revids: set[int]) -> dict[int, PageSnapshot | None]: ...

    def leads(self, revids: set[int]) -> dict[int, Lead]: ...


class WikiPages:
    """Revisions from the MediaWiki API, through the caches spec 07a and 07b fill."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.cache = settings.data_dir / "cache" / "ml"

    def _client(self):
        from wikipulse.eval.wiki_client import WikiClient

        s = self.settings
        return WikiClient(s.user_agent, self.cache, s.wiki_api_min_interval_s)

    def snapshots(self, revids: set[int]) -> dict[int, PageSnapshot | None]:
        from wikipulse.ml.pages import (
            RevisionCache,
            fetch_creation_times,
            fetch_revisions,
            snapshot_of,
        )

        with self._client() as client:
            url = self.settings.wiki_api_url
            revisions = fetch_revisions(
                client, url, revids, RevisionCache(self.cache / "revisions")
            )
            pageids = {r.pageid for r in revisions.values() if r.pageid is not None}
            created = fetch_creation_times(client, url, pageids)
        return {
            revid: snapshot_of(rev, created.get(rev.pageid) if rev.pageid else None)
            for revid, rev in revisions.items()
        }

    def leads(self, revids: set[int]) -> dict[int, Lead]:
        from wikipulse.ml.summaries import LeadCache, fetch_leads

        with self._client() as client:
            cache = LeadCache(self.cache / "leads")
            return fetch_leads(client, self.settings.wiki_api_url, revids, cache)


# --- scoring --------------------------------------------------------------------------


@dataclass
class Prepared:
    spike: SpikeToScore
    window: SpikeWindow
    edits: Sequence[PageEdit]
    revision: int | None


def model_inputs(
    prepared: Sequence[Prepared],
    snapshots: dict[int, PageSnapshot | None],
    leads: dict[int, Lead] | None,
) -> tuple[pd.DataFrame, list[dict[str, float]]]:
    """The model's input table (`INPUT_COLUMNS`) and each row's features.

    `default_rule_fired` is True: the spikes served come from a detector with the
    default thresholds (the live one), so that rule has fired for every one of them by
    its start. `summary_text` is the page's lead at the cutoff for a model that reads it
    (`leads` given), else just the title (ignored by the other models).
    """
    rows, features = [], []
    for item in prepared:
        page = snapshots.get(item.revision) if item.revision is not None else None
        values = check_no_leakage(item.window, item.edits, page)  # also proves the cutoff
        features.append(values)
        if leads is not None:
            lead = leads.get(item.revision) if item.revision is not None else None
            text = summary_text(item.spike.title, lead)
        else:
            text = item.spike.title
        rows.append({**values, RULE_COLUMN: True, TEXT_COLUMN: text})
    frame = pd.DataFrame(rows, columns=INPUT_COLUMNS)
    numeric = [c for c in INPUT_COLUMNS if c not in (RULE_COLUMN, TEXT_COLUMN)]
    frame[numeric] = frame[numeric].astype("float64")
    frame[RULE_COLUMN] = frame[RULE_COLUMN].astype(bool)
    return frame, features


def score_spikes(
    model: LoadedModel,
    spikes: Sequence[SpikeToScore],
    edits_of: Callable[[str], Sequence[PageEdit]],
    pages: PageSource,
    window_min: float,
    history_start: datetime | None,
) -> list[SpikeScore]:
    """Score spikes at their cutoff. `edits_of(title)` gives the page's known edits (any
    after the cutoff are ignored); `history_start` is the earliest moment the edit source
    covers, to record whether a spike's 24 hours of history were complete."""
    if not spikes:
        return []
    prepared = []
    for spike in spikes:
        window = SpikeWindow(spike.title, spike.first_edit_at, spike.started_at, window_min)
        edits = edits_of(spike.title)
        prepared.append(Prepared(spike, window, edits, revision_at(edits, window.cutoff)))
    revids = {p.revision for p in prepared if p.revision is not None}
    snapshots = pages.snapshots(revids) if revids else {}
    leads = (pages.leads(revids) if revids else {}) if model.needs_text else None
    frame, features = model_inputs(prepared, snapshots, leads)
    out = model.predict(frame)
    scores = []
    for item, values, score, is_news in zip(
        prepared, features, out["score"], out["is_news"], strict=True
    ):
        complete = (
            history_start is not None and item.window.first_edit_at - HISTORY >= history_start
        )
        scores.append(
            SpikeScore(
                spike_id=item.spike.id,
                run_id=item.spike.run_id,
                model_name=model.name,
                model_version=model.version,
                score=float(score),
                is_news=bool(is_news),
                threshold=model.threshold,
                cutoff=item.window.cutoff,
                features=values,
                page_revision_id=item.revision,
                history_complete=complete,
            )
        )
    return scores


def log_scores(scores: Sequence[SpikeScore], titles: dict[int, str]) -> None:
    for s in scores:
        logger.info(
            "%s %.3f %s (v%s, cutoff %s)",
            "NEWS    " if s.is_news else "not news",
            s.score,
            titles.get(s.spike_id, s.spike_id),
            s.model_version,
            s.cutoff.strftime("%m-%d %H:%M"),
        )


# --- live: edits from the stream ------------------------------------------------------


def page_edit(event: EditEvent) -> PageEdit:
    """The part of an edit the features use (as `dataset.page_edits` reads the lake)."""
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


@dataclass
class EditBuffer:
    """The last `keep` of edits per page, in event time.

    `start` is the earliest moment from which the buffer holds *every* edit: the oldest
    event it has seen, moved forward when old edits are pruned. A spike whose burst began
    before `start` can't be scored correctly; one whose 24 hours of history began before
    it is scored with incomplete history (and flagged so).
    """

    keep: timedelta
    pages: dict[str, list[PageEdit]] = field(default_factory=lambda: defaultdict(list))
    start: datetime | None = None
    watermark: datetime | None = None  # the newest event time seen

    def add(self, event: EditEvent) -> None:
        self.pages[event.title].append(page_edit(event))
        moment = event.event_time
        if self.start is None or moment < self.start:
            self.start = moment
        if self.watermark is None or moment > self.watermark:
            self.watermark = moment

    def edits(self, title: str) -> list[PageEdit]:
        return sorted(self.pages.get(title, ()), key=lambda e: e.time)

    def prune(self) -> int:
        """Forget edits older than `keep` before the watermark; returns how many."""
        if self.watermark is None:
            return 0
        horizon = self.watermark - self.keep
        dropped = 0
        for title in list(self.pages):
            kept = [e for e in self.pages[title] if e.time >= horizon]
            dropped += len(self.pages[title]) - len(kept)
            if kept:
                self.pages[title] = kept
            else:
                del self.pages[title]
        if self.start is not None and self.start < horizon:
            self.start = horizon
        return dropped

    def size(self) -> int:
        return sum(len(edits) for edits in self.pages.values())


def stream_start_id(client: Any, stream: str, keep: timedelta) -> str:
    """Where to start reading so the buffer gets `keep` of history: the newest entry's
    id minus `keep` (entry ids are milliseconds), or the beginning of the stream."""
    newest = client.xrevrange(stream, count=1)
    if not newest:
        return "0-0"
    millis = int(newest[0][0].split("-")[0]) - int(keep.total_seconds() * 1000)
    return f"{max(millis, 0)}-0"


class LiveScorer:
    """Reads the edit stream into an `EditBuffer` and scores spikes past their cutoff."""

    def __init__(
        self,
        client: Any,  # redis.Redis with decode_responses=True
        conn: Any,  # psycopg.Connection in autocommit mode
        stream: str,
        run_id: str,
        holder: ModelHolder,
        pages: PageSource,
        window_min: float,
        lateness: timedelta,
        keep: timedelta,
        poll_s: float = 30,
        batch: int = 1000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.conn = conn
        self.stream = stream
        self.run_id = run_id
        self.holder = holder
        self.pages = pages
        self.window_min = window_min
        self.lateness = lateness
        self.buffer = EditBuffer(keep)
        self.poll_s = poll_s
        self.batch = batch
        self.clock = clock
        self.last_id: str | None = None
        self.scored = 0
        self._last_poll = float("-inf")

    def read(self, block_ms: int | None = None) -> int:
        """Add the next entries of the stream to the buffer; returns how many were read."""
        from wikipulse.lake.archiver import parse_entry

        if self.last_id is None:
            self.last_id = stream_start_id(self.client, self.stream, self.buffer.keep)
        response = self.client.xread({self.stream: self.last_id}, count=self.batch, block=block_ms)
        entries = response[0][1] if response else []
        for entry_id, fields in entries:
            self.last_id = entry_id
            event = parse_entry(entry_id, fields)
            if event is not None:
                self.buffer.add(event)
        return len(entries)

    def catch_up(self) -> int:
        """Read everything the stream holds now (the buffer's history at start)."""
        total = 0
        while (count := self.read()) > 0:
            total += count
        logger.info(
            "Edit buffer: %d edits of %d pages, %s to %s",
            self.buffer.size(),
            len(self.buffer.pages),
            self.buffer.start,
            self.buffer.watermark,
        )
        return total

    def score_due(self, lateness: timedelta | None = None) -> int:
        """Score every unscored spike whose cutoff the stream has passed by `lateness`."""
        if self.buffer.watermark is None:
            return 0
        ready = self.buffer.watermark - (self.lateness if lateness is None else lateness)
        spikes = spikes_to_score(
            self.conn, self.run_id, self.window_min, ready, first_edit_from=self.buffer.start
        )
        if not spikes:
            return 0
        model = self.holder.current()
        scores = score_spikes(
            model, spikes, self.buffer.edits, self.pages, self.window_min, self.buffer.start
        )
        upsert_scores(self.conn, scores)
        log_scores(scores, {s.id: s.title for s in spikes})
        self.scored += len(scores)
        return len(scores)

    def step(self, block_ms: int) -> int:
        """Read once (waiting up to `block_ms`), then score if a poll is due."""
        count = self.read(block_ms)
        if self.clock() - self._last_poll >= self.poll_s:
            self._last_poll = self.clock()
            self.score_due()
            self.buffer.prune()
        return count

    def run(self, stop: Callable[[], bool], exit_when_idle: float | None = None) -> int:
        self.catch_up()
        self.holder.current()  # fail early if there is no champion
        idle_since = self.clock()
        while not stop():
            if self.step(block_ms=int(min(self.poll_s, 5) * 1000)) > 0:
                idle_since = self.clock()
            elif exit_when_idle is not None and self.clock() - idle_since >= exit_when_idle:
                # A finished replay: nothing more will arrive, so score up to its end.
                self.score_due(lateness=timedelta(0))
                break
        logger.info("Scored %d spikes of run %s", self.scored, self.run_id)
        return self.scored


# --- backfill: edits from the lake ----------------------------------------------------


def backfill(
    conn: Any,
    settings: Settings,
    model: LoadedModel,
    pages: PageSource,
    run_id: str,
    lake_prefix: str,
    since: datetime | None = None,
    until: datetime | None = None,
    rescore: bool = False,
) -> list[SpikeScore]:
    """Score a past run's spikes with edits from the lake (all whose burst and cutoff
    lie inside the archived hours)."""
    from wikipulse.lake.store import LakeStore
    from wikipulse.ml.dataset import page_edits

    lake = LakeStore.from_settings(settings, lake_prefix)
    hours = sorted(lake.hours())
    if not hours:
        raise ValueError(f"no edits in the lake at {lake.root}")
    start, end = hours[0], hours[-1] + timedelta(hours=1)
    window = settings.feature_window_min
    spikes = spikes_to_score(
        conn, run_id, window, end, start, since, until, rescore=rescore, limit=1_000_000
    )
    logger.info("Backfill: %d spikes of run %s to score from %s", len(spikes), run_id, lake.root)
    if not spikes:
        return []
    edits = page_edits(lake, {s.title for s in spikes})
    scores = score_spikes(model, spikes, lambda t: edits.get(t, []), pages, window, start)
    upsert_scores(conn, scores)
    log_scores(scores, {s.id: s.title for s in spikes})
    return scores


# --- the command ----------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    from wikipulse.ml.dataset import parse_moment

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model-name", help="registered model (default ML_MODEL_NAME)")
    parser.add_argument("--tracking-uri", help="MLflow tracking URI (default MLFLOW_TRACKING_URI)")
    commands = parser.add_subparsers(dest="command")
    live = commands.add_parser("live", help="score new spikes from the edit stream (default)")
    live.add_argument("--stream-key", help="Redis Stream to read (default STREAM_KEY)")
    live.add_argument("--run-id", help="detector run to score (default ML_SERVE_RUN_ID)")
    live.add_argument(
        "--exit-when-idle", type=float, metavar="SECONDS", help="stop when the stream is quiet"
    )
    past = commands.add_parser("backfill", help="score a past run's spikes from the lake")
    past.add_argument("--run-id", required=True, help="detector run, e.g. 07a-default")
    past.add_argument("--lake-prefix", help="lake prefix (default ML_LAKE_PREFIX)")
    past.add_argument("--since", type=parse_moment, help="spikes started at/after (UTC)")
    past.add_argument("--until", type=parse_moment, help="spikes started before (UTC)")
    past.add_argument("--rescore", action="store_true", help="also re-score scored spikes")
    args = parser.parse_args(argv)
    if args.command is None:  # `serve` alone, or `serve --model-name x`: live
        args = parser.parse_args([*(sys.argv[1:] if argv is None else argv), "live"])
    return args


def main(argv: list[str] | None = None) -> int:
    import psycopg
    import redis

    from wikipulse.detect.run import StopFlag

    args = parse_args(argv)
    settings = get_settings()
    setup_logging(settings.log_level)
    mlflow.set_tracking_uri(args.tracking_uri or settings.mlflow_tracking_uri)
    name = args.model_name or settings.ml_model_name
    pages = WikiPages(settings)
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as conn:
        if args.command == "backfill":
            model = load_alias(name)
            logger.info("Scoring with %s version %s (%s)", name, model.version, model.model_type)
            prefix = args.lake_prefix or settings.ml_lake_prefix
            scores = backfill(
                conn,
                settings,
                model,
                pages,
                args.run_id,
                prefix,
                args.since,
                args.until,
                args.rescore,
            )
            news = sum(s.is_news for s in scores)
            print(f"Scored {len(scores)} spikes of run {args.run_id}: {news} flagged as news")
            return 0
        client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
        scorer = LiveScorer(
            client,
            conn,
            args.stream_key or settings.stream_key,
            args.run_id or settings.ml_serve_run_id,
            ModelHolder(name, reload_s=settings.ml_serve_reload_s),
            pages,
            settings.feature_window_min,
            timedelta(seconds=settings.ml_serve_lateness_s),
            timedelta(hours=settings.ml_serve_buffer_hours),
            poll_s=settings.ml_serve_poll_s,
        )
        stop = StopFlag()
        stop.install()
        scorer.run(stop, args.exit_when_idle)
    return 0


if __name__ == "__main__":
    sys.exit(main())

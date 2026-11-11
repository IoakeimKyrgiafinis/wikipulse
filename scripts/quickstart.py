"""WikiPulse in one command, offline: `uv run python scripts/quickstart.py`.

Starts the minimal system with Docker Compose (Redis, Postgres and an empty Neo4j; no
Kubernetes), replays the bundled sample (`demo/sample/`: a recorded day of English
Wikipedia edits, 5 November 2026) through the real pipeline, and opens the live page:

    sample ──► stream.replay ──► Redis Stream ──► detect.run ──► Postgres spikes
                                                                     │
    browser ◄── WebSocket ◄── api (the live page) ◄── events ◄── events.run (grouping)

1. `docker compose up -d --wait redis postgres neo4j`, then the database migrations. (The
   page reads headlines and entities from Neo4j; without `--graph` it stays empty.)
2. Loads the sample's enriched pages (summaries, links) into the `pages` table, instead
   of running the enricher, which would call the Wikipedia API.
3. Replays the 12 hours before the day at full speed (the detector's warm-up: it learns
   each page's normal edit rate), then starts grouping and the page, then replays the
   day itself `--speed` times faster than real time (default 300: a day in about five
   minutes), so events appear on the page as they are detected.
4. Keeps the page running until Ctrl+C (`--exit-when-done` stops after the day instead).

Nothing contacts Wikimedia: every Wikimedia URL of the started processes points at a
local address (`--graph`: the offline stand-in `scripts/fake_wikimedia.py`), and the
live consumer and the enricher are never started. The only download is the grouping's
embedding model (`EVENTS_EMBED_MODEL`, from Hugging Face) on the very first run; once it
is cached, the quickstart runs without any network.

`--graph` fills the knowledge graph: the graph runner extracts each event
with the local LLM (`EXTRACT_MODEL`, Ollama `qwen3:4b`), so cards gain a headline and
entities. Offline, entities are not linked to Wikidata and the burst text is not
fetched (the model reads the page summaries only). Needs Ollama with the model pulled.

Run it again at any time: the run `quickstart` (spikes, events, stream) starts from
scratch. The containers keep running afterwards; `docker compose stop` stops them.
Ports and the Compose project come from the usual settings (`REDIS_PORT`/`REDIS_URL`,
`POSTGRES_PORT`/`POSTGRES_DSN`, `API_PORT`, `COMPOSE_PROJECT_NAME`, ...).
"""

import argparse
import gzip
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO

import httpx
import psycopg
import redis

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # so `wikipulse` imports when run as a script

from wikipulse.config import Settings, get_settings  # noqa: E402
from wikipulse.db.pages import get_page, upsert_page  # noqa: E402
from wikipulse.enrich.pages import PageInfo  # noqa: E402
from wikipulse.eval import gate  # noqa: E402
from wikipulse.logging_setup import setup_logging  # noqa: E402
from wikipulse.stream.models import EditEvent  # noqa: E402
from wikipulse.stream.replay import parse_speed  # noqa: E402
from wikipulse.stream.sources import format_recording_line  # noqa: E402

logger = logging.getLogger("quickstart")

SAMPLE_DIR = ROOT / "demo" / "sample"
EDITS_FILE = "edits.jsonl.gz"
PAGES_FILE = "pages.jsonl.gz"
RUN_ID = "quickstart"  # the detector run and the grouping run
STREAM_KEY = "wiki:quickstart:edits"  # its own stream: never the live or replay stream
DEFAULT_SPEED = "300"
# Where Wikimedia URLs point without --graph: a loopback port nothing listens on
# (9 is "discard"), so an accidental call fails at once instead of reaching Wikimedia.
NOWHERE = "http://127.0.0.1:9"
WIKIMEDIA_URL_SETTINGS = (
    "STREAM_URL",
    "WIKI_API_URL",
    "WIKI_REST_URL",
    "WIKIDATA_API_URL",
    "WIKIDATA_SPARQL_URL",
)
COMPOSE_TIMEOUT_S = 600
API_START_TIMEOUT_S = 120
PROGRESS_INTERVAL_S = 15
SETTLE_POLLS = 3  # --exit-when-done: unchanged counts this many polls in a row


# --- The sample -----------------------------------------------------------------------


def raw_message(number: int, edit: EditEvent) -> str:
    """The sample edit as a `recentchange` message, the shape the live stream sends."""
    message = {
        "meta": {
            "id": f"quickstart-{number}",
            "dt": edit.event_time.isoformat().replace("+00:00", "Z"),
            "domain": "en.wikipedia.org",
        },
        "wiki": "enwiki",
        "server_name": "en.wikipedia.org",
        "namespace": 0,
        "type": edit.type,
        "title": edit.title,
        "user": edit.user,
        "bot": edit.is_bot,
        "minor": edit.is_minor,
        "comment": edit.comment,
    }
    return json.dumps(message, ensure_ascii=False)


def write_recording(path: Path, edits: Iterable[EditEvent], first_number: int = 0) -> int:
    """A recording file (the format of `wikipulse.stream.record`) of the edits."""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as out:
        for number, edit in enumerate(edits, start=first_number):
            line = format_recording_line(
                f"quickstart-{number}", raw_message(number, edit), edit.event_time
            )
            out.write(line + "\n")
            count += 1
    return count


def split_sample(
    info: gate.FixtureInfo, edits: Sequence[EditEvent]
) -> tuple[list[EditEvent], list[EditEvent]]:
    """The warm-up edits (before the shown day) and the day's edits."""
    warmup = [e for e in edits if e.event_time < info.evaluate_from]
    return warmup, list(edits[len(warmup) :])


def read_pages(path: Path) -> list[PageInfo]:
    """The sample's enriched pages."""
    with gzip.open(path, "rt", encoding="utf-8") as lines:
        return [PageInfo.model_validate_json(line) for line in lines if line.strip()]


def load_pages(conn: psycopg.Connection, pages: Iterable[PageInfo]) -> int:
    """Insert the pages the table doesn't have yet (existing rows are never replaced)."""
    added = 0
    for page in pages:
        if get_page(conn, page.requested_title) is None:
            upsert_page(conn, page)
            added += 1
    return added


# --- Settings for the started processes -------------------------------------------------


def child_env(base: dict[str, str], wikimedia_url: str, hf_offline: bool) -> dict[str, str]:
    """The environment of every started process: offline by construction.

    Every Wikimedia URL points at `wikimedia_url` (a local address), grouping doesn't
    wait for an enricher that isn't running, and the page and grouping poll often so the
    fast replay looks smooth.
    """
    env = dict(base)
    env.update({name: wikimedia_url for name in WIKIMEDIA_URL_SETTINGS})
    env.update(
        {
            "EVENTS_ENRICH_WAIT_S": "0",
            "EVENTS_POLL_INTERVAL_S": "2",
            "API_POLL_INTERVAL_S": "2",
            "GRAPH_POLL_INTERVAL_S": "5",
            "PYTHONUTF8": "1",
        }
    )
    env.pop("METRICS_PORT", None)  # one metrics port can't serve several processes
    if hf_offline:
        env["HF_HUB_OFFLINE"] = "1"
    return env


def model_is_cached(model: str) -> bool:
    """True if the embedding model is already in the Hugging Face cache."""
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(model, local_files_only=True)
    except Exception:  # noqa: BLE001 - any failure means "not usable offline"
        return False
    return True


# --- Processes --------------------------------------------------------------------------


@dataclass
class Child:
    name: str
    process: subprocess.Popen
    log: IO[str]


class Children:
    """The started WikiPulse processes, each logging to `<log_dir>/<name>.log`."""

    def __init__(self, log_dir: Path, env: dict[str, str]) -> None:
        self.log_dir = log_dir
        self.env = env
        self.running: list[Child] = []
        log_dir.mkdir(parents=True, exist_ok=True)

    def start(self, name: str, args: Sequence[str]) -> Child:
        log = (self.log_dir / f"{name}.log").open("w", encoding="utf-8")
        command = [sys.executable, *args]
        logger.info("Starting %s: %s", name, " ".join(args))
        process = subprocess.Popen(command, cwd=ROOT, env=self.env, stdout=log, stderr=log)
        child = Child(name, process, log)
        self.running.append(child)
        return child

    def run(self, name: str, args: Sequence[str]) -> None:
        """Run a process to its end; raise if it fails."""
        child = self.start(name, args)
        if child.process.wait() != 0:
            raise SystemExit(f"{name} failed; see {self.log_dir / (name + '.log')}")
        self.running.remove(child)
        child.log.close()

    def check(self) -> None:
        """Stop with a message if a background process died."""
        for child in self.running:
            if child.process.poll() not in (None, 0):
                raise SystemExit(
                    f"{child.name} stopped (exit code {child.process.returncode}); "
                    f"see {self.log_dir / (child.name + '.log')}"
                )

    def stop_all(self) -> None:
        for child in reversed(self.running):
            if child.process.poll() is None:
                child.process.terminate()
        for child in reversed(self.running):
            try:
                child.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.process.kill()
            child.log.close()
        self.running.clear()


def compose(services: Sequence[str]) -> None:
    """Start Compose services and wait until their health checks pass."""
    command = ["docker", "compose", "up", "-d", "--wait", *services]
    logger.info("Starting %s with Docker Compose (the first time downloads the images)",
                ", ".join(services))  # fmt: skip
    result = subprocess.run(command, cwd=ROOT, timeout=COMPOSE_TIMEOUT_S, check=False)
    if result.returncode != 0:
        raise SystemExit("docker compose up failed; is Docker Desktop running?")


def check_tools() -> None:
    """Docker with Compose must be installed and running."""
    if shutil.which("docker") is None:
        raise SystemExit("Docker is not installed: https://www.docker.com/products/docker-desktop/")
    info = subprocess.run(["docker", "info"], capture_output=True, check=False, timeout=60)
    if info.returncode != 0:
        raise SystemExit("Docker is not running: start Docker Desktop and try again.")


def port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex((host, port)) != 0


def wait_for_api(url: str, children: Children, timeout_s: float = API_START_TIMEOUT_S) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        children.check()
        try:
            if httpx.get(f"{url}/health", timeout=10).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise SystemExit(f"The API did not answer at {url} within {timeout_s:.0f} s")


# --- Progress ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Progress:
    spikes: int
    events: int
    multi_page: int
    extracted: int
    data_until: datetime | None

    def line(self) -> str:
        until = f"{self.data_until:%b %d, %H:%M} UTC" if self.data_until else "-"
        text = f"data up to {until}: {self.spikes} spikes, {self.events} events "
        text += f"({self.multi_page} with 2+ pages)"
        return text + (f", {self.extracted} in the graph" if self.extracted else "")


PROGRESS_SQL = """
SELECT (SELECT count(*) FROM spikes WHERE run_id = %(run)s AND started_at >= %(since)s),
       (SELECT count(*) FROM events WHERE run_id = %(run)s AND status <> 'merged'),
       (SELECT count(*) FROM events
         WHERE run_id = %(run)s AND status <> 'merged' AND page_count >= 2),
       (SELECT count(*) FROM graph_extractions WHERE run_id = %(run)s AND status = 'ok'),
       (SELECT max(last_edit_at) FROM spikes WHERE run_id = %(run)s)
"""

TOP_EVENTS_SQL = """
SELECT number, title, page_count, started_at FROM events
WHERE run_id = %s AND status <> 'merged'
ORDER BY page_count DESC, score DESC LIMIT %s
"""


def progress(conn: psycopg.Connection, since: datetime) -> Progress:
    """How far the run has got; spikes of the warm-up are not counted."""
    row = conn.execute(PROGRESS_SQL, {"run": RUN_ID, "since": since}).fetchone()
    assert row is not None
    return Progress(*row)


def wait_until_settled(
    conn: psycopg.Connection, children: Children, since: datetime, graph: bool
) -> Progress:
    """After the replay: wait until detection, grouping (and extraction) stop changing."""
    last, same = None, 0
    while same < SETTLE_POLLS:
        time.sleep(PROGRESS_INTERVAL_S)
        children.check()
        current = progress(conn, since)
        pending = graph and current.extracted < current.events
        same = same + 1 if current == last and not pending else 0
        if current != last:
            logger.info(current.line())
        last = current
    assert last is not None
    return last


# --- Main -------------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--speed",
        type=parse_speed,
        default=parse_speed(DEFAULT_SPEED),
        help=f"replay speed-up of the day: a factor (default {DEFAULT_SPEED}) or 'max'",
    )
    parser.add_argument("--graph", action="store_true", help="add Neo4j and LLM extraction")
    parser.add_argument("--no-browser", action="store_true", help="don't open the page")
    parser.add_argument(
        "--exit-when-done", action="store_true", help="stop once the day is replayed and grouped"
    )
    parser.add_argument("--fake-wikimedia-port", type=int, default=8089, help="--graph only")
    parser.add_argument("--sample", type=Path, default=SAMPLE_DIR, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def prepare_sample(sample: Path, data_dir: Path) -> tuple[gate.FixtureInfo, Path, Path, int]:
    """Write the sample's warm-up and day as recordings under `data_dir`."""
    info, edits = gate.read_fixture(sample / EDITS_FILE)
    warmup, day = split_sample(info, edits)
    out = data_dir / "quickstart"
    warmup_file, day_file = out / "warmup.jsonl", out / "day.jsonl"
    write_recording(warmup_file, warmup)
    write_recording(day_file, day, first_number=len(warmup))
    logger.info(
        "Sample: %d edits, warm-up from %s, the day %s to %s",
        len(edits), f"{info.start:%b %d %H:%M}", f"{info.evaluate_from:%b %d %H:%M}",
        f"{info.end:%b %d %H:%M} UTC",
    )  # fmt: skip
    return info, warmup_file, day_file, len(day)


def start_graph(children: Children, settings: Settings, port: int) -> None:
    """--graph: the offline Wikimedia stand-in and the graph runner."""
    if not port_is_free("127.0.0.1", port):
        raise SystemExit(f"Port {port} is in use: pick another with --fake-wikimedia-port")
    children.run("llm-check", ["-m", "wikipulse.llm.check", "--model", settings.extract_model])
    children.start(
        "fake-wikimedia",
        ["scripts/fake_wikimedia.py", "--host", "127.0.0.1", "--port", str(port)],
    )
    children.start("graph", ["-m", "wikipulse.graph.run", "--run-id", RUN_ID, "--reset"])


def run(args: argparse.Namespace, children: Children, settings: Settings) -> Progress:
    url = f"http://127.0.0.1:{settings.api_port}"
    if not port_is_free("127.0.0.1", settings.api_port):
        raise SystemExit(f"Port {settings.api_port} is in use: set API_PORT to a free one")
    compose(["redis", "postgres", "neo4j"])
    children.run("migrate", ["-m", "wikipulse.db.migrate"])
    info, warmup_file, day_file, day_edits = prepare_sample(args.sample, settings.data_dir)
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as conn:
        added = load_pages(conn, read_pages(args.sample / PAGES_FILE))
        logger.info("Loaded %d enriched pages (pages already there are kept)", added)
    client = redis.Redis.from_url(settings.redis_url)
    try:
        client.delete(STREAM_KEY)  # a rerun starts from an empty stream
    finally:
        client.close()

    replay = ["-m", "wikipulse.stream.replay", "--stream-key", STREAM_KEY, "--file"]
    children.run("replay-warmup", [*replay, str(warmup_file)])
    children.start(
        "detect",
        ["-m", "wikipulse.detect.run", "--stream-key", STREAM_KEY, "--run-id", RUN_ID, "--reset"],
    )
    since = info.evaluate_from.isoformat()
    children.start(
        "events",
        ["-m", "wikipulse.events.run", "--spike-run", RUN_ID, "--since", since, "--reset"],
    )
    if args.graph:
        start_graph(children, settings, args.fake_wikimedia_port)
    children.start("api", ["-m", "wikipulse.api", "--run-id", RUN_ID])
    wait_for_api(url, children)
    logger.info("The page: %s", url)
    if not args.no_browser:
        webbrowser.open(url)

    speed = "max" if args.speed is None else f"{args.speed:g}"
    logger.info("Replaying the day (%d edits) at speed %s", day_edits, speed)
    day = children.start("replay-day", [*replay, str(day_file), "--speed", speed])
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as conn:
        while day.process.poll() is None:
            time.sleep(PROGRESS_INTERVAL_S)
            children.check()
            logger.info(progress(conn, info.evaluate_from).line())
        if day.process.returncode != 0:
            raise SystemExit("The replay failed; see its log")
        logger.info("The day is replayed; waiting for detection and grouping to finish")
        final = wait_until_settled(conn, children, info.evaluate_from, args.graph)
        for number, title, pages, started in conn.execute(TOP_EVENTS_SQL, (RUN_ID, 5)):
            logger.info("  event %d: %s (%d pages, from %s)", number, title, pages,
                        f"{started:%H:%M} UTC")  # fmt: skip
    if not args.exit_when_done:
        logger.info("Done. The page stays at %s until Ctrl+C", url)
        while True:
            time.sleep(5)
            children.check()
    return final


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging()
    os.chdir(ROOT)  # Compose, the sample and DATA_DIR are relative to the repository
    check_tools()
    settings = get_settings()
    hf_offline = model_is_cached(settings.events_embed_model)
    if not hf_offline:
        logger.info("First run: grouping downloads its embedding model from Hugging Face")
    wikimedia = f"http://127.0.0.1:{args.fake_wikimedia_port}" if args.graph else NOWHERE
    log_dir = settings.data_dir / "quickstart" / "logs"
    children = Children(log_dir, child_env(dict(os.environ), wikimedia, hf_offline))
    logger.info("Logs of the started processes: %s", log_dir)
    try:
        final = run(args, children, settings)
    except KeyboardInterrupt:
        logger.info("Stopping")
        return 0
    finally:
        children.stop_all()
    logger.info("Finished: %s", final.line())
    return 0


if __name__ == "__main__":
    sys.exit(main())

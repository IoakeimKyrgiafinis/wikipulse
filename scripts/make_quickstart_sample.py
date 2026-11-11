"""Make the quickstart's sample (spec 10) from the history lake and the `pages` table.

    uv run python scripts/make_quickstart_sample.py \
        --start 2026-11-04T12:00Z --show-from 2026-11-05T00:00Z --end 2026-11-06T00:00Z

Runs once, on the laptop (it reads the lake prefix `ML_LAKE_PREFIX`, `history/edits`, and
Postgres); what it writes is committed and replayed by `scripts/quickstart.py`, so a
fresh clone can show WikiPulse without a connection to Wikimedia:

1. **The edits** `demo/sample/edits.jsonl.gz`, in the detection gate's compact format
   (`wikipulse.eval.gate`, spec 09c): the human edits of English Wikipedia articles
   between `--start` and `--end`, users numbered, edit summaries reduced to a revert
   flag, pages with fewer than three edits left out. The first line says where the
   warm-up ends (`evaluate_from` = `--show-from`): the quickstart replays the warm-up
   at full speed, so the detector has baselines, and then the shown day at `--speed`.
2. **The pages** `demo/sample/pages.jsonl.gz`: the enriched page (spec 03a: summary,
   description, Wikidata id, categories, links) of every page the detector variants
   `VARIANTS` raise a spike on, as the `pages` table holds it. The quickstart loads them
   instead of running the enricher, which would call the Wikipedia API. Pages the table
   doesn't have are counted and left out; grouping then sees their title only.

Both files are written byte for byte the same from the same input (gzip time 0).
"""

import argparse
import gzip
import logging
import sys
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # so `wikipulse` imports when run as a script
sys.path.insert(0, str(ROOT / "scripts"))

from make_gate_fixture import MIN_PAGE_EDITS, compact_lines, human_edits  # noqa: E402

from wikipulse.config import DetectorConfig, get_settings  # noqa: E402
from wikipulse.db.pages import get_page  # noqa: E402
from wikipulse.enrich.pages import PageInfo  # noqa: E402
from wikipulse.eval import gate  # noqa: E402
from wikipulse.lake.store import LakeStore  # noqa: E402
from wikipulse.logging_setup import setup_logging  # noqa: E402
from wikipulse.stream.models import EditEvent  # noqa: E402

logger = logging.getLogger("make_quickstart_sample")

SAMPLE_DIR = Path("demo") / "sample"
EDITS_FILE = "edits.jsonl.gz"
PAGES_FILE = "pages.jsonl.gz"
# Pages for the spikes of the default thresholds and of spec 02b's medium variant, so a
# reader who loosens the thresholds a little still gets summaries.
VARIANTS: dict[str, dict[str, int]] = {
    "default": {},
    "medium": {"min_edits": 4, "min_editors": 3, "long_min_edits": 8, "long_min_editors": 3},
}


def hours(start: datetime, end: datetime) -> list[datetime]:
    """The whole hours that overlap [start, end)."""
    found, hour = [], start.replace(minute=0, second=0, microsecond=0)
    while hour < end:
        found.append(hour)
        hour += timedelta(hours=1)
    return found


def lake_edits(store: LakeStore, start: datetime, end: datetime) -> list[EditEvent]:
    """The archived edits of [start, end), in event-time order."""
    events: list[EditEvent] = []
    for hour in hours(start, end):
        table = store.read_hour(hour)
        if table is None:
            raise SystemExit(f"The lake has no file for {hour:%Y-%m-%d %H}:00 UTC")
        events += [EditEvent.model_validate(row) for row in table.to_pylist()]
    chosen = [e for e in events if start <= e.event_time < end]
    return sorted(chosen, key=lambda e: e.event_time)


def spike_titles(events: Sequence[EditEvent], show_from: datetime) -> list[str]:
    """Titles of the spikes the variants raise, sorted (warm-up spikes included)."""
    titles: set[str] = set()
    for name, overrides in VARIANTS.items():
        spikes = gate.detect(events, DetectorConfig(**overrides))
        shown = [s for s in spikes if s.first_edit_at >= show_from]
        logger.info("Variant %s: %d spikes, %d in the shown hours", name, len(spikes), len(shown))
        titles |= {s.title for s in spikes}
    return sorted(titles)


def write_pages(path: Path, pages: Iterable[PageInfo]) -> None:
    """One `PageInfo` per line, gzip with a fixed header time."""
    text = "".join(page.model_dump_json() + "\n" for page in pages)
    with path.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as out:
        out.write(text.encode("utf-8"))


def parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", type=parse_time, required=True, help="warm-up begins")
    parser.add_argument("--show-from", type=parse_time, required=True, help="shown day begins")
    parser.add_argument("--end", type=parse_time, required=True, help="edits before this")
    parser.add_argument("--out", type=Path, default=SAMPLE_DIR)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging()
    logging.getLogger("wikipulse.detect").setLevel(logging.WARNING)
    settings = get_settings()
    store = LakeStore.from_settings(settings, settings.ml_lake_prefix)
    chosen = human_edits(lake_edits(store, args.start, args.end), args.start, args.end)
    note = (
        f"Human edits of English Wikipedia articles with at least {MIN_PAGE_EDITS} in the "
        "period; users numbered, edit summaries reduced to a revert flag. "
        "evaluate_from: where the quickstart's warm-up ends."
    )
    source = f"lake {settings.s3_bucket}/{settings.ml_lake_prefix}"
    info = gate.FixtureInfo(source, args.start, args.show_from, args.end, len(chosen), note)
    args.out.mkdir(parents=True, exist_ok=True)
    gate.write_fixture(args.out / EDITS_FILE, info, compact_lines(chosen))
    logger.info("Wrote %d edits to %s", len(chosen), args.out / EDITS_FILE)

    _, sample = gate.read_fixture(args.out / EDITS_FILE)  # exactly what the replay sends
    titles = spike_titles(sample, args.show_from)
    with psycopg.connect(settings.postgres_dsn) as conn:
        found = {title: get_page(conn, title) for title in titles}
    pages = [page for page in found.values() if page is not None]
    missing = [title for title, page in found.items() if page is None]
    write_pages(args.out / PAGES_FILE, pages)
    logger.info("Wrote %d of %d pages to %s", len(pages), len(titles), args.out / PAGES_FILE)
    if missing:
        logger.info("Not enriched (title only): %s", ", ".join(missing))
    return 0


if __name__ == "__main__":
    sys.exit(main())

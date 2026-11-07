"""How quickly were a run's spikes enriched? `uv run python scripts/enrich_latency.py RUN_ID`.

For every spike of the run whose page was first enriched after the spike started, prints
the delay from the spike's `started_at` (event time, which for a live run is within
seconds of the wall clock) to the page's `enriched_at`. That delay includes the
detector's snapshot interval (DETECT_SNAPSHOT_INTERVAL_S: spikes reach Postgres only at
snapshots), the enrichment poll interval and the API calls.
"""

import statistics
import sys

import psycopg

from wikipulse.config import get_settings

QUERY = """
SELECT s.title, extract(epoch FROM p.enriched_at - s.started_at) AS delay_s
FROM spikes s JOIN pages p ON p.title = s.title
WHERE s.run_id = %s AND p.enriched_at >= s.started_at
ORDER BY s.started_at
"""


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    with psycopg.connect(get_settings().postgres_dsn) as conn:
        rows = conn.execute(QUERY, (argv[0],)).fetchall()
    if not rows:
        print("No enriched spikes for this run.")
        return 1
    delays = sorted(float(delay) for _, delay in rows)
    within = sum(delay <= 60 for delay in delays)
    print(f"{len(delays)} spikes enriched after they started")
    print(f"delay median {statistics.median(delays):.1f} s, max {delays[-1]:.1f} s")
    print(f"within 60 s: {within} of {len(delays)}")
    for title, delay in rows:
        print(f"  {float(delay):6.1f} s  {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""The whole evaluation against the real Postgres (portal pages from fixtures, no network).

Run with `uv run pytest -m integration`.
"""

import json
import uuid
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import psycopg
import pytest

from tests.eval.helpers import at, spike
from tests.eval.test_ground_truth import load
from wikipulse.config import get_settings
from wikipulse.db.migrate import migrate
from wikipulse.db.spikes import delete_run, upsert_spikes
from wikipulse.eval import detection
from wikipulse.eval.ground_truth import PortalDay, parse_portal_response
from wikipulse.eval.label_store import append_record, record_for

pytestmark = pytest.mark.integration


@pytest.fixture
def run_id() -> Iterator[str]:
    rid = f"test-eval-{uuid.uuid4().hex[:8]}"
    with psycopg.connect(get_settings().postgres_dsn, autocommit=True) as conn:
        migrate(conn)
        yield rid
        delete_run(conn, rid)


def fake_portal(client, api_url: str, day: date) -> PortalDay:
    if day == date(2026, 11, 6):
        return parse_portal_response(day, load("portal-2026-11-06.json"))
    return PortalDay(day, [], [], missing=True)


def test_evaluate_writes_both_reports(
    run_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(detection, "fetch_portal_day", fake_portal)
    spikes = [
        spike("Thomas Markle", at(6, 18), delay_min=7),
        spike("Pedro Alliana", at(6, 23), delay_min=11),
        spike("Some Film", at(6, 12)),
        spike("Outside the range", at(9, 1)),
    ]
    labels = tmp_path / "labels.jsonl"
    append_record(labels, record_for(spikes[2], run_id, "news", "llm", at(7), model="m"))
    with psycopg.connect(get_settings().postgres_dsn, autocommit=True) as conn:
        upsert_spikes(conn, run_id, spikes)
    args = detection.parse_args(
        ["--from", "2026-11-06", "--to", "2026-11-06", "--name", run_id, "--set", "min_edits=4"]
        + ["--labels", str(labels), "--results-dir", str(tmp_path / "results")]
    )
    md_path = detection.evaluate(args, get_settings())

    report = json.loads(md_path.with_name(md_path.name[:-3] + ".json").read_text("utf-8"))
    m = report["metrics"]
    assert m["spikes"] == 3  # the spike on Nov 9 is outside --from/--to
    assert m["spikes_on_portal"] == 2
    assert m["links"] == 107 and m["links_found"] == 2
    assert m["labeled_spikes"] == 1 and m["label_precision"] == 1.0
    assert m["ttd_median_min"] == pytest.approx(9)
    assert report["config"]["min_edits"] == 4
    assert "Detection evaluation" in md_path.read_text(encoding="utf-8")

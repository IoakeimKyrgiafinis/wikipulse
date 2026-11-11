"""scripts/quickstart.py and its bundled sample (spec 10), without Docker or a network."""

import asyncio
import importlib.util
import io
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wikipulse.config import DetectorConfig, Settings
from wikipulse.enrich.pages import PageInfo, PageStatus
from wikipulse.eval import gate
from wikipulse.stream.filters import is_wanted, normalize
from wikipulse.stream.models import EditEvent
from wikipulse.stream.sources import FileSource

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "quickstart.py"
spec = importlib.util.spec_from_file_location("quickstart", SCRIPT)
assert spec is not None and spec.loader is not None
quickstart = importlib.util.module_from_spec(spec)
sys.modules["quickstart"] = quickstart
spec.loader.exec_module(quickstart)

WIKIMEDIA_HOSTS = ("wikimedia.org", "wikipedia.org", "wikidata.org")


def edit(minute: int, title: str = "Storm Amy", revert: bool = False) -> EditEvent:
    moment = datetime(2026, 11, 5, 16, minute, 30, 250000, tzinfo=UTC)
    return EditEvent(
        event_id=f"x-{minute}",
        event_time=moment,
        received_at=moment,
        title=title,
        type="edit",
        user="user-7",
        is_bot=False,
        is_minor=False,
        rev_old=None,
        rev_new=None,
        len_old=None,
        len_new=None,
        comment=gate.REVERT_COMMENT if revert else "",
    )


# --- The sample as a recording ---


def test_a_sample_edit_becomes_a_message_the_consumer_keeps() -> None:
    original = edit(3, revert=True)
    raw = json.loads(quickstart.raw_message(41, original))
    assert is_wanted(raw)
    event = normalize(raw, received_at=original.event_time)
    assert event.event_id == "quickstart-41"
    assert event.event_time == original.event_time
    assert (event.title, event.user, event.is_bot) == ("Storm Amy", "user-7", False)
    assert event.comment == gate.REVERT_COMMENT


def test_the_recording_replays_in_order_with_its_event_times(tmp_path: Path) -> None:
    path = tmp_path / "day.jsonl"
    edits = [edit(1), edit(2, "Pedro Alliana"), edit(5)]
    assert quickstart.write_recording(path, edits, first_number=100) == 3

    async def read() -> list[tuple[str | None, str]]:
        return [message async for message in FileSource(path).messages()]

    messages = asyncio.run(read())
    assert [sse_id for sse_id, _ in messages] == [
        "quickstart-100",
        "quickstart-101",
        "quickstart-102",
    ]
    events = [normalize(json.loads(data)) for _, data in messages]
    assert [e.event_time for e in events] == [e.event_time for e in edits]
    assert [e.title for e in events] == ["Storm Amy", "Pedro Alliana", "Storm Amy"]


def test_the_sample_splits_at_the_end_of_the_warm_up() -> None:
    edits = [edit(1), edit(2), edit(9)]
    info = gate.FixtureInfo(
        "test", edits[0].event_time, edits[2].event_time, datetime(2026, 11, 6, tzinfo=UTC), 3
    )
    warmup, day = quickstart.split_sample(info, edits)
    assert (warmup, day) == (edits[:2], edits[2:])


# --- Offline by construction ---


def test_every_wikimedia_url_points_at_the_local_address() -> None:
    env = quickstart.child_env({"METRICS_PORT": "9464", "PATH": "x"}, "http://127.0.0.1:9", True)
    wikimedia_settings = {
        name.upper()
        for name, field in Settings.model_fields.items()
        if isinstance(field.default, str) and any(h in field.default for h in WIKIMEDIA_HOSTS)
    }
    assert wikimedia_settings == set(quickstart.WIKIMEDIA_URL_SETTINGS)
    for name in wikimedia_settings:
        assert env[name] == "http://127.0.0.1:9"
    assert env["EVENTS_ENRICH_WAIT_S"] == "0"  # no enricher: grouping doesn't wait for it
    assert float(env["DETECT_SNAPSHOT_INTERVAL_S"]) <= 5  # spikes reach the page quickly
    assert env["HF_HUB_OFFLINE"] == "1"
    assert "METRICS_PORT" not in env  # one port can't serve several processes
    assert env["PATH"] == "x"


def test_the_hugging_face_cache_is_left_alone_when_the_model_is_missing() -> None:
    env = quickstart.child_env({}, quickstart.NOWHERE, hf_offline=False)
    assert "HF_HUB_OFFLINE" not in env


def test_pages_already_there_are_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    stored = {"Ueno Bank": PageInfo(requested_title="Ueno Bank", title="Ueno Bank", status="ok")}
    written: list[str] = []
    monkeypatch.setattr(quickstart, "get_page", lambda conn, title: stored.get(title))
    monkeypatch.setattr(quickstart, "upsert_page", lambda conn, page: written.append(page.title))
    pages = [
        PageInfo(requested_title=t, title=t, status=PageStatus.OK)
        for t in ("Ueno Bank", "Pedro Alliana")
    ]
    assert quickstart.load_pages(object(), pages) == 1
    assert written == ["Pedro Alliana"]


# --- Settling and the started processes ---


class NoChildren:
    def check(self) -> None:
        pass


def settle(
    monkeypatch: pytest.MonkeyPatch, polls: list[tuple["quickstart.Progress", int]], graph: bool
) -> tuple["quickstart.Progress", int]:
    """Run wait_until_settled over scripted polls; return its result and the polls used."""
    used = 0

    def next_poll(conn: object, since: datetime) -> "quickstart.Progress":
        nonlocal used
        used += 1
        return polls[min(used, len(polls)) - 1][0]

    monkeypatch.setattr(quickstart.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(quickstart, "progress", next_poll)
    monkeypatch.setattr(
        quickstart, "extractions_waiting", lambda conn: polls[min(used, len(polls)) - 1][1]
    )
    final = quickstart.wait_until_settled(object(), NoChildren(), datetime(2026, 11, 5), graph)
    return final, used


def counts(extracted: int, failed: int) -> "quickstart.Progress":
    return quickstart.Progress(74, 69, 12, extracted, failed, datetime(2026, 11, 6))


def test_a_failed_extraction_does_not_keep_the_graph_run_from_settling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 68 extracted, 1 failed (its retry is minutes of wall clock away): nothing is waiting.
    final, used = settle(monkeypatch, [(counts(68, 1), 0)], graph=True)
    assert (final.extracted, final.failed) == (68, 1)
    assert used == quickstart.SETTLE_POLLS + 1
    assert "1 failed extraction(s)" in final.line()


def test_the_graph_run_settles_only_when_no_extraction_is_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    polls = [(counts(60, 0), 9)] * 5 + [(counts(69, 0), 0)]
    final, used = settle(monkeypatch, polls, graph=True)
    assert final.extracted == 69
    assert used == 5 + quickstart.SETTLE_POLLS + 1  # unchanged counts alone were not enough


def test_failed_extractions_never_wait_for_their_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def pending(conn: object, run_id: str, retry: timedelta) -> list[int]:
        seen.update(run_id=run_id, retry=retry)
        return [3, 8]

    monkeypatch.setattr(quickstart, "pending_events", pending)
    assert quickstart.extractions_waiting(object()) == 2
    assert seen["run_id"] == quickstart.RUN_ID
    assert seen["retry"] >= timedelta(days=1)  # longer than any quickstart run


class Finished:
    def __init__(self, code: int | None) -> None:
        self.returncode = code

    def poll(self) -> int | None:
        return self.returncode


def children_with(tmp_path: Path, code: int | None, may_finish: bool) -> "quickstart.Children":
    children = quickstart.Children(tmp_path, {})
    children.running.append(quickstart.Child("detect", Finished(code), io.StringIO(), may_finish))
    return children


def test_a_service_that_ends_stops_the_quickstart_even_with_exit_code_0(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="detect stopped"):
        children_with(tmp_path, 0, may_finish=False).check()
    with pytest.raises(SystemExit, match="exit code 1"):
        children_with(tmp_path, 1, may_finish=True).check()


def test_a_job_may_end_and_a_running_service_is_fine(tmp_path: Path) -> None:
    children_with(tmp_path, 0, may_finish=True).check()
    children_with(tmp_path, None, may_finish=False).check()


# --- The committed sample ---


def test_the_bundled_sample_is_small_and_anonymous() -> None:
    sample = quickstart.SAMPLE_DIR
    size = sum(path.stat().st_size for path in sample.glob("*.gz"))
    assert size < 3_000_000
    info, edits = gate.read_fixture(sample / quickstart.EDITS_FILE)
    assert info.start < info.evaluate_from < info.end
    assert all(e.user.startswith("user-") and e.comment in ("", gate.REVERT_COMMENT) for e in edits)


def test_every_spike_of_the_shown_day_has_its_page() -> None:
    info, edits = gate.read_fixture(quickstart.SAMPLE_DIR / quickstart.EDITS_FILE)
    pages = {
        p.requested_title for p in quickstart.read_pages(quickstart.SAMPLE_DIR / "pages.jsonl.gz")
    }
    spikes = gate.detect(edits, DetectorConfig())
    shown = [s for s in spikes if s.first_edit_at >= info.evaluate_from]
    assert len(shown) > 50
    assert {s.title for s in shown} <= pages

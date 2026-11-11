"""The detection eval gate (spec 09c): its scoring, its baseline check, and the committed
fixture, labels and baseline, which CI's `eval-gate` job runs."""

import gzip
import json
from datetime import timedelta
from pathlib import Path

import pytest

from tests.eval.helpers import at, spike
from wikipulse.config import DetectorConfig, Settings
from wikipulse.detect.detector import is_revert
from wikipulse.eval import gate
from wikipulse.eval.label_store import load_labels, load_records
from wikipulse.eval.metrics import Label
from wikipulse.stream.models import EditEvent

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / gate.DEFAULT_FIXTURE
LABELS = ROOT / gate.DEFAULT_LABELS
BASELINE = ROOT / gate.DEFAULT_BASELINE


def label(title: str, start_hour: float, value: str, labeler: str = "llm") -> Label:
    first = at(7, start_hour) - timedelta(minutes=5)
    return Label(title, first, at(7, start_hour + 1), value, labeler, "ollama:test")


def edit(title: str, minute: int, user: str = "Alice", comment: str = "") -> EditEvent:
    moment = at(7, 1) + timedelta(minutes=minute)
    return EditEvent(
        event_id=f"id-{title}-{minute}", event_time=moment, received_at=moment, title=title,
        type="edit", user=user, is_bot=False, is_minor=False, rev_old=1, rev_new=2,
        len_old=10, len_new=20, comment=comment,
    )  # fmt: skip


# --- The fixture format ---


def test_a_fixture_round_trips_and_keeps_the_revert_flag(tmp_path: Path) -> None:
    info = gate.FixtureInfo("rec.jsonl", at(7, 0), at(7, 1), at(7, 2), 2, "note")
    lines = [
        gate.compact_edit(edit("A", 1), 1, revert=False),
        gate.compact_edit(edit("A", 2, comment="Reverted edits by X"), 2, revert=True),
    ]
    path = tmp_path / "edits.jsonl.gz"
    gate.write_fixture(path, info, lines)
    read_info, events = gate.read_fixture(path)
    assert read_info == info
    assert [e.title for e in events] == ["A", "A"]
    assert [e.event_time for e in events] == [at(7, 1) + timedelta(minutes=m) for m in (1, 2)]
    assert [e.user for e in events] == ["user-1", "user-2"]
    assert [is_revert(e.comment) for e in events] == [False, True]
    assert not any(e.is_bot for e in events)


def test_the_same_fixture_gives_the_same_bytes(tmp_path: Path) -> None:
    info = gate.FixtureInfo("rec.jsonl", at(7, 0), at(7, 1), at(7, 2), 1)
    lines = [gate.compact_edit(edit("A", 1), 1, revert=False)]
    gate.write_fixture(tmp_path / "a.gz", info, lines)
    gate.write_fixture(tmp_path / "b.gz", info, lines)
    assert (tmp_path / "a.gz").read_bytes() == (tmp_path / "b.gz").read_bytes()


def test_a_fixture_with_a_wrong_count_is_refused(tmp_path: Path) -> None:
    info = gate.FixtureInfo("rec.jsonl", at(7, 0), at(7, 1), at(7, 2), 5)
    gate.write_fixture(tmp_path / "x.gz", info, [gate.compact_edit(edit("A", 1), 1, False)])
    with pytest.raises(ValueError, match="1 edits"):
        gate.read_fixture(tmp_path / "x.gz")


# --- Scoring ---


def test_precision_counts_unlabeled_spikes_as_not_news_and_leaves_out_unsure() -> None:
    spikes = [
        spike("News", at(7, 2)),
        spike("Noise", at(7, 3)),
        spike("Unknown", at(7, 4)),
        spike("Maybe", at(7, 5)),
    ]
    labels = [
        label("News", 2, "news"),
        label("Noise", 3, "not news"),
        label("Maybe", 5, "unsure"),
    ]
    result = gate.score(spikes, labels, at(7, 1), at(7, 12))
    assert result.counts == {"news": 1, "not news": 1, "unsure": 1, "unlabeled": 1}
    assert result.precision == pytest.approx(1 / 3)
    assert [s.title for s in result.unlabeled] == ["Unknown"]


def test_a_human_label_beats_an_llm_label() -> None:
    labels = [label("A", 2, "not news"), label("A", 2, "news", labeler="human")]
    result = gate.score([spike("A", at(7, 2))], labels, at(7, 1), at(7, 12))
    assert result.counts["news"] == 1


def test_recall_is_the_share_of_news_bursts_some_spike_covers() -> None:
    labels = [
        label("Found", 2, "news"),
        label("Missed", 3, "news"),
        label("Noise", 4, "not news"),
        label("Outside", 20, "news"),  # after the evaluated hours: not counted
    ]
    spikes = [spike("Found", at(7, 2) + timedelta(minutes=30)), spike("Noise", at(7, 4))]
    result = gate.score(spikes, labels, at(7, 1), at(7, 12))
    assert (result.found, result.news_bursts) == (1, 2)
    assert result.recall == pytest.approx(0.5)
    assert result.found_bursts == [gate.burst_key(labels[0])]


def test_only_spikes_that_began_in_the_window_are_scored() -> None:
    early = spike("Warmup", at(7, 0) + timedelta(minutes=30))  # burst began before 01:00
    result = gate.score([early], [], at(7, 1), at(7, 12))
    assert result.spikes == 0


# --- The baseline check ---


def baseline(precision: float | None, recall: float | None, margin: float = 0.05) -> dict:
    return {"precision": precision, "recall": recall, "margin": margin}


def result_with(news: int, not_news: int, found: int, bursts: int) -> gate.GateResult:
    counts = {"news": news, "not news": not_news, "unsure": 0, "unlabeled": 0}
    return gate.GateResult(news + not_news, counts, bursts, found)


def test_a_drop_within_the_margin_passes_and_a_bigger_one_fails() -> None:
    assert gate.check(result_with(5, 5, 4, 10), baseline(0.54, 0.44)) == []
    failures = gate.check(result_with(4, 6, 4, 10), baseline(0.5, 0.4))
    assert len(failures) == 1 and failures[0].startswith("precision dropped from 0.500 to 0.400")
    failures = gate.check(result_with(5, 5, 3, 10), baseline(0.5, 0.4))
    assert len(failures) == 1 and failures[0].startswith("recall dropped")


def test_better_numbers_pass() -> None:
    assert gate.check(result_with(9, 1, 9, 10), baseline(0.5, 0.4)) == []


def test_changed_inputs_make_the_baseline_stale(tmp_path: Path) -> None:
    fixture, labels = tmp_path / "f.gz", tmp_path / "labels.jsonl"
    fixture.write_bytes(b"x")
    labels.write_bytes(b'{"a": 1}\r\n')
    data = {"fixture_sha256": gate.file_digest(fixture), "labels_sha256": gate.file_digest(labels)}
    assert gate.stale_inputs(data, fixture, labels) == []
    labels.write_bytes(b'{"a": 1}\n')  # line endings alone don't count
    assert gate.stale_inputs(data, fixture, labels) == []
    labels.write_bytes(b'{"a": 2}\n')
    assert gate.stale_inputs(data, fixture, labels) == [f"{labels} changed"]


def test_overrides_are_parsed_like_the_detection_eval() -> None:
    assert gate.parse_overrides(["min_edits=4,min_editors=2", "quiet_period_min=10"]) == {
        "min_edits": "4", "min_editors": "2", "quiet_period_min": "10",
    }  # fmt: skip
    with pytest.raises(SystemExit):
        gate.parse_overrides(["min_edits"])


# --- The committed gate ---


@pytest.fixture(scope="module")
def committed() -> tuple[gate.FixtureInfo, list[EditEvent], list[Label], dict]:
    info, events = gate.read_fixture(FIXTURE)
    return info, events, load_labels(LABELS), json.loads(BASELINE.read_text("utf-8"))


def run_gate(committed: tuple, config: DetectorConfig) -> tuple[gate.GateResult, list[str]]:
    info, events, labels, stored = committed
    result = gate.score(gate.detect(events, config), labels, info.evaluate_from, info.end)
    return result, gate.check(result, stored)


def test_the_fixture_is_small_and_has_no_user_names() -> None:
    assert FIXTURE.stat().st_size < 400_000
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as lines:
        next(lines)
        first = json.loads(next(lines))
    assert set(first) == {"t", "p", "u", "r"} and isinstance(first["u"], int)


def test_the_baseline_matches_the_committed_inputs(committed: tuple) -> None:
    *_, stored = committed
    assert gate.stale_inputs(stored, FIXTURE, LABELS) == []


def test_the_settings_defaults_reproduce_the_baseline(committed: tuple) -> None:
    *_, stored = committed
    config = Settings(_env_file=None).detector_config()
    assert config.model_dump() == stored["config"]
    result, failures = run_gate(committed, config)
    assert failures == []
    assert result.precision == pytest.approx(stored["precision"])
    assert result.recall == pytest.approx(stored["recall"])
    assert result.counts["unlabeled"] == 0


def test_a_looser_threshold_fails_on_precision(committed: tuple) -> None:
    config = DetectorConfig(min_edits=4, long_min_edits=8, long_min_editors=3)
    result, failures = run_gate(committed, config)
    assert result.recall >= committed[3]["recall"]  # it finds more ...
    assert [f.split(" ")[0] for f in failures] == ["precision"]  # ... but mostly noise


def test_a_stricter_threshold_fails_on_recall(committed: tuple) -> None:
    config = DetectorConfig(min_edits=6, min_editors=4, long_min_edits=12, long_min_editors=5)
    _, failures = run_gate(committed, config)
    assert "recall" in [f.split(" ")[0] for f in failures]


def test_every_gate_label_states_its_labeler(committed: tuple) -> None:
    records = load_records(LABELS)
    assert records and all(r.labeler in ("llm", "human") for r in records)
    assert all(r.model for r in records if r.labeler == "llm")


def test_the_cli_passes_on_the_committed_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(ROOT)
    for name in list(Settings.model_fields):
        if name.startswith("detect_"):
            monkeypatch.delenv(name.upper(), raising=False)
    summary = tmp_path / "summary.md"
    assert gate.main(["--markdown", str(summary)]) == 0
    assert "Detection eval gate: PASS" in summary.read_text("utf-8")
    assert gate.main(["--set", "count_reverts=true"]) == 1

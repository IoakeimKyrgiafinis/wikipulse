"""The evaluation report: building it, writing Markdown, comparing two runs."""

import json
from datetime import date
from pathlib import Path

import pytest

from tests.eval.helpers import at, portal_day, spike
from wikipulse.config import DetectorConfig, Settings
from wikipulse.eval.detection import (
    build_report,
    compare_reports,
    config_snapshot,
    find_report,
    load_portal,
    parse_args,
    parse_overrides,
    render_markdown,
)
from wikipulse.eval.metrics import Label, spike_end

GIT = {"commit": "abc123", "dirty": False}


def make_report(name: str = "baseline", extra_spikes: int = 0) -> dict:
    portal = [
        portal_day(5, ["Old Story"], ["Old Story"]),
        portal_day(6, ["Storm Ava", "France", "Jane Doe"], ["Storm Ava", "Jane Doe"]),
        portal_day(7, ["Election"], ["Election"]),
        portal_day(8, [], []),
    ]
    spikes = [
        spike("Storm Ava", at(6, 9), delay_min=4),
        spike("Jane Doe", at(6, 15), delay_min=12),
        spike("TV Show", at(6, 20)),
        spike("Election", at(7, 2), delay_min=8, status="active"),
    ]
    spikes += [spike(f"Noise {n}", at(7, 5)) for n in range(extra_spikes)]
    labels = [
        Label("TV Show", spikes[2].first_edit_at, spike_end(spikes[2]), "news", "llm"),
        Label("Storm Ava", spikes[0].first_edit_at, spike_end(spikes[0]), "news", "human"),
    ]
    config = DetectorConfig().model_dump()
    return build_report(
        name, name, date(2026, 11, 6), date(2026, 11, 7), spikes, portal, labels, config,
        GIT, at(8, 12),
    )  # fmt: skip


def test_report_metrics() -> None:
    m = make_report()["metrics"]
    assert m["spikes"] == 4
    assert m["links"] == 4 and m["links_found"] == 3  # France had no spike
    assert m["recall_all"] == pytest.approx(3 / 4)
    assert m["leads"] == 3 and m["recall_lead"] == pytest.approx(1.0)
    assert m["spikes_on_portal"] == 3
    assert m["portal_precision"] == pytest.approx(3 / 4)
    assert m["labeled_spikes"] == 2 and m["label_precision"] == 1.0
    assert m["labelers"] == {"human": 1, "llm": 1}
    assert m["ttd_count"] == 3
    assert m["ttd_median_min"] == pytest.approx(8)


def test_report_has_a_per_day_breakdown_config_and_commit() -> None:
    report = make_report()
    per_day = {d["day"]: d for d in report["per_day"]}
    assert set(per_day) == {"2026-11-06", "2026-11-07"}
    assert per_day["2026-11-06"]["spikes"] == 3
    assert per_day["2026-11-06"]["spikes_on_portal"] == 2
    assert per_day["2026-11-06"]["recall_all"] == pytest.approx(2 / 3)
    assert per_day["2026-11-07"]["found_leads"] == ["Election"]
    assert report["config"]["min_edits"] == DetectorConfig().min_edits
    assert report["git"]["commit"] == "abc123"
    json.dumps(report)  # everything is JSON-ready


def test_markdown_states_the_caveats_and_numbers() -> None:
    text = render_markdown(make_report())
    assert "# Detection evaluation: baseline" in text
    assert "**lower bound**" in text
    assert "**estimate**" in text
    assert "75.0%" in text  # recall over all links and portal precision
    assert "`abc123`" in text
    assert "| min_edits | 5 |" in text
    assert "| 2026-11-06 |" in text


def test_compare_prints_differences_and_changed_settings() -> None:
    a = make_report("strict")
    b = make_report("loose", extra_spikes=4)
    b["config"]["min_edits"] = 3
    rows = {row[0]: row for row in compare_reports(a, b)}
    assert rows["spikes"][1:] == ("4.0", "8.0", "+4.0")
    assert rows["portal precision (lower bound)"][1:] == ("75.0%", "37.5%", "-37.5 pp")
    assert rows["config: min_edits"][1:3] == ("5", "3")
    assert "config: min_editors" not in rows


def test_find_report_takes_the_newest_with_that_name(tmp_path: Path) -> None:
    for name in ("2026-11-07_loose.json", "2026-11-08_loose.json", "2026-11-08_strict.json"):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    assert find_report("loose", tmp_path).name == "2026-11-08_loose.json"
    assert find_report(str(tmp_path / "2026-11-07_loose.json"), tmp_path).name.startswith(
        "2026-11-07"
    )
    with pytest.raises(FileNotFoundError):
        find_report("missing", tmp_path)


def test_config_snapshot_applies_overrides() -> None:
    settings = Settings(_env_file=None)
    config = config_snapshot(settings, parse_overrides(["min_edits=3,min_editors=2"]))
    assert config["min_edits"] == 3 and config["min_editors"] == 2
    assert config["long_min_edits"] == DetectorConfig().long_min_edits


def test_future_portal_days_are_not_fetched() -> None:
    class NoNetwork:
        def get_json(self, *args, **kwargs):
            raise AssertionError("must not fetch")

    today = date(2026, 11, 8)
    days = load_portal(NoNetwork(), "unused", date(2026, 11, 9), date(2026, 11, 10), today)
    assert all(day.missing for day in days)


def test_arguments() -> None:
    args = parse_args(["--from", "2026-11-05", "--to", "2026-11-06", "--name", "base"])
    assert args.date_from == date(2026, 11, 5) and args.run_id is None
    assert parse_args(["--compare", "a", "b"]).compare == ["a", "b"]
    with pytest.raises(SystemExit):
        parse_args(["--from", "2026-11-05"])


def test_spikes_before_the_range_count_for_recall_only() -> None:
    portal = [portal_day(6, ["France"], ["France"])]
    spikes = [spike("France", at(5, 22))]  # started the evening before --from
    report = build_report(
        "r", "r", date(2026, 11, 6), date(2026, 11, 6), spikes, portal, [], {}, GIT, at(8)
    )
    assert report["metrics"]["recall_all"] == 1.0
    assert report["metrics"]["spikes"] == 0
    assert report["spikes"] == []


def test_label_precision_uses_only_the_runs_random_sample() -> None:
    spikes = [spike(f"Page {n}", at(6, n / 10)) for n in range(60)]
    labels = [Label(s.title, s.first_edit_at, spike_end(s), "news", "llm") for s in spikes]
    report = build_report(
        "r", "r", date(2026, 11, 6), date(2026, 11, 6), spikes, [], labels, {}, GIT, at(8),
        sample_size=10, seed=1,
    )  # fmt: skip
    m = report["metrics"]
    assert (m["label_sample"], m["labeled_spikes"], m["label_seed"]) == (10, 10, 1)
    assert sum(d["labeled_spikes"] for d in report["per_day"]) == 0  # no portal days given

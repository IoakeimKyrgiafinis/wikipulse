"""Heartbeats and the probe command (spec 08a)."""

import os
from pathlib import Path

import pytest

from wikipulse import health
from wikipulse.api.__main__ import listen_host
from wikipulse.config import Settings
from wikipulse.health import Heartbeat, beat_while, check


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_beat_creates_the_file(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "heartbeat"
    Heartbeat(path).beat()
    assert path.exists()


def test_beats_are_throttled(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat"
    clock = FakeClock()
    heartbeat = Heartbeat(path, min_interval_s=5, clock=clock)
    heartbeat.beat()
    os.utime(path, (0, 0))  # pretend the touch happened long ago
    clock.now = 4.0
    heartbeat.beat()  # within the interval: not touched again
    assert path.stat().st_mtime == 0
    clock.now = 5.0
    heartbeat.beat()
    assert path.stat().st_mtime > 0


def test_disabled_heartbeat_does_nothing(tmp_path: Path) -> None:
    Heartbeat(None).beat()  # no error, no file
    assert list(tmp_path.iterdir()) == []


def test_from_settings_reads_health_file(tmp_path: Path) -> None:
    path = tmp_path / "hb"
    assert Heartbeat.from_settings(Settings(_env_file=None, health_file=path)).path == path
    assert Heartbeat.from_settings(Settings(_env_file=None, health_file=None)).path is None


def test_beat_survives_an_unwritable_path(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    Heartbeat(blocker / "heartbeat").beat()  # parent is a file: logged, not raised


def test_beat_while_beats_and_passes_the_answer_through(tmp_path: Path) -> None:
    path = tmp_path / "hb"
    answers = iter([False, True])
    should_stop = beat_while(lambda: next(answers), Heartbeat(path, min_interval_s=0))
    assert should_stop() is False
    assert path.exists()
    assert should_stop() is True


@pytest.mark.parametrize(
    ("age", "healthy"),
    [(10, True), (299, True), (301, False)],
)
def test_check_compares_the_age(tmp_path: Path, age: float, healthy: bool) -> None:
    path = tmp_path / "hb"
    path.touch()
    os.utime(path, (1000, 1000))
    ok, explanation = check(path, 300, now=lambda: 1000 + age)
    assert ok is healthy
    assert "s ago" in explanation


def test_check_without_a_file(tmp_path: Path) -> None:
    ok, explanation = check(tmp_path / "missing", 300)
    assert not ok
    assert "no heartbeat yet" in explanation


def test_probe_command_exit_codes(tmp_path: Path) -> None:
    path = tmp_path / "hb"
    assert health.main(["--file", str(path)]) == 1
    path.touch()
    assert health.main(["--file", str(path), "--max-age", "60"]) == 0


def test_listen_host_needs_the_switch_for_all_interfaces() -> None:
    assert listen_host("127.0.0.1", listen_all=False) == "127.0.0.1"
    assert listen_host("0.0.0.0", listen_all=True) == "0.0.0.0"
    with pytest.raises(SystemExit):
        listen_host("0.0.0.0", listen_all=False)
    with pytest.raises(SystemExit):  # the switch allows "every interface", nothing else
        listen_host("10.0.0.5", listen_all=True)

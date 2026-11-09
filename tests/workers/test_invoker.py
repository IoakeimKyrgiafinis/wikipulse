"""Retries, the dead-letter list and redrive."""

import fakeredis
import pytest

from tests.workers.helpers import event_for
from wikipulse.workers.invoker import DeadLetter, Invoker, dead_letters, redrive


class Flaky:
    """A handler that fails its first `failures` calls, then succeeds."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def __call__(self, event) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError(f"boom {self.calls}")


@pytest.fixture
def client() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis(decode_responses=True)


def make(handler, client, **kwargs) -> tuple[Invoker, list[float]]:
    waits: list[float] = []
    kwargs.setdefault("max_attempts", 3)
    kwargs.setdefault("retry_delay_s", 2.0)
    return Invoker(handler, client, "dead", sleep=waits.append, **kwargs), waits


def test_success_needs_no_retry(client) -> None:
    invoker, waits = make(Flaky(0), client)
    assert invoker.invoke(event_for("b", "k")) is True
    assert waits == [] and client.llen("dead") == 0


def test_a_failure_is_retried_with_growing_delays(client) -> None:
    handler = Flaky(2)
    invoker, waits = make(handler, client)
    assert invoker.invoke(event_for("b", "k")) is True
    assert handler.calls == 3
    assert waits == [2.0, 4.0]
    assert invoker.stats.retries == 2 and client.llen("dead") == 0


def test_a_deliberately_failing_handler_ends_up_in_the_dead_letter_list(client) -> None:
    def always_fails(event) -> None:
        raise ValueError("deliberate failure")

    invoker, waits = make(always_fails, client)
    event = event_for("bucket", "edits/year=2026/month=11/day=07/hour=12/part-0.parquet")
    assert invoker.invoke(event) is False
    assert len(waits) == 2  # 3 attempts
    [letter] = dead_letters(client, "dead")
    assert letter.event == event
    assert letter.attempts == 3
    assert letter.error == "ValueError: deliberate failure"
    assert letter.handler == "hourly_stats"
    assert invoker.stats.dead_lettered == 1


def test_one_attempt_means_no_retry(client) -> None:
    handler = Flaky(1)
    invoker, waits = make(handler, client, max_attempts=1)
    assert invoker.invoke(event_for("b", "k")) is False
    assert handler.calls == 1 and waits == [] and client.llen("dead") == 1


def test_dead_letter_round_trip() -> None:
    letter = DeadLetter("h", event_for("b", "k"), 3, "E: x", "2026-11-09T16:00:00+00:00")
    assert DeadLetter.from_json(letter.to_json()) == letter


def test_redrive_retries_each_letter_once(client) -> None:
    failing = Flaky(100)
    invoker, _ = make(failing, client)
    invoker.invoke(event_for("b", "first"))
    invoker.invoke(event_for("b", "second"))
    assert client.llen("dead") == 2

    still_failing, _ = make(failing, client)
    assert redrive(still_failing) == (0, 2)  # no endless loop; both back in the list
    assert client.llen("dead") == 2

    fixed, _ = make(Flaky(0), client)
    assert redrive(fixed) == (2, 0)
    assert client.llen("dead") == 0


def test_max_attempts_must_be_positive(client) -> None:
    with pytest.raises(ValueError):
        Invoker(Flaky(0), client, "dead", max_attempts=0)

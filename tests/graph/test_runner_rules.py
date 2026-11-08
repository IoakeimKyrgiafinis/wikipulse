"""Unit tests: when the graph runner extracts an event again, and which events leave the graph."""

from wikipulse.db.graph import event_fingerprint, needs_extraction
from wikipulse.graph.run import stale_events


def test_an_active_event_has_a_different_fingerprint_than_the_same_event_ended() -> None:
    assert event_fingerprint("3,7", "ended") == "3,7"
    assert event_fingerprint("3,7", "active") == "3,7|active"


def test_an_event_extracted_while_active_is_extracted_again_when_it_ends() -> None:
    stored = event_fingerprint("3,7", "active")
    assert not needs_extraction("3,7", "active", stored, retry_due=None, force=False)
    assert needs_extraction("3,7", "ended", stored, retry_due=None, force=False)
    # ... and only once: after that the stored fingerprint matches again.
    ended = event_fingerprint("3,7", "ended")
    assert not needs_extraction("3,7", "ended", ended, retry_due=None, force=False)


def test_new_changed_forced_and_retry_due_events_are_extracted() -> None:
    assert needs_extraction("3", "ended", None, retry_due=None, force=False)  # never tried
    assert needs_extraction("3,9", "ended", "3", retry_due=False, force=False)  # spike joined
    assert needs_extraction("3", "ended", "3", retry_due=False, force=True)
    assert needs_extraction("3", "ended", "3", retry_due=True, force=False)
    assert not needs_extraction("3", "ended", "3", retry_due=False, force=False)


def test_merged_and_deleted_events_are_stale() -> None:
    graph_keys = ["run-a:1", "run-a:2", "run-a:5"]
    extracted = [1, 2, 3]  # 3 failed, so it has a row but no node
    current = [1, 4]  # 2 and 3 merged away, 5 deleted from Postgres, 4 not extracted yet
    assert stale_events("run-a", graph_keys, extracted, current) == [2, 3, 5]


def test_stale_events_ignore_keys_of_other_runs() -> None:
    assert stale_events("run-a", ["run-ab:1", "run-a:1"], [], [1]) == []

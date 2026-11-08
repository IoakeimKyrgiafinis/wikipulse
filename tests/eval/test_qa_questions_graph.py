"""Integration test: generating the question set from the frozen snapshot (read only).

Needs the `neo4j-eval` service (`docker compose --profile eval up -d`, filled by
`scripts/snapshot_graph.py restore`); skipped when it isn't reachable. Writes nothing.
"""

import pytest
from neo4j.exceptions import AuthError, ServiceUnavailable

from wikipulse.config import get_settings
from wikipulse.eval.qa_questions import QUESTION_TYPES, Snapshot, generate

pytestmark = pytest.mark.integration


def test_generation_is_deterministic_and_covers_every_type() -> None:
    settings = get_settings()
    snapshot = Snapshot.connect(settings.eval_qa_neo4j_uri, settings)
    try:
        try:
            snapshot.driver.verify_connectivity()
        except (ServiceUnavailable, AuthError, OSError):
            pytest.skip("the snapshot server (neo4j-eval) is not running")
        first = generate(snapshot, "test")
        second = generate(snapshot, "test")
    finally:
        snapshot.close()
    assert first == second
    assert len(first) == 25
    assert {t: sum(q.type == t for q in first) for t in QUESTION_TYPES} == dict.fromkeys(
        QUESTION_TYPES, 5
    )
    assert all(q.answerable == (q.type != "unanswerable") for q in first)
    assert all(q.expected_entities or q.expected_events for q in first if q.type == "event")
    assert all(qid.startswith("Q") for q in first for qid in q.expected_entities)

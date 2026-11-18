"""The shared read-only cases (tests/fixtures/cypher_cases.json) against the Python validator.

The same file is run against the browser's port, `site/cypher-check.js`, by
tests/test_demo_graph_js.py (spec 13a): both must refuse and rewrite exactly alike.
"""

import json
import re
from pathlib import Path

import pytest

from wikipulse.qa.cypher import UnsafeCypherError, validate_cypher

CASES_FILE = Path(__file__).resolve().parents[1] / "fixtures" / "cypher_cases.json"
CASES = json.loads(CASES_FILE.read_text(encoding="utf-8"))["validate"]


def check_case(case: dict) -> None:
    """Assert one shared case against `validate_cypher`."""
    max_rows = case.get("max_rows", 25)
    if "error" in case:
        with pytest.raises(UnsafeCypherError) as raised:
            validate_cypher(case["query"], max_rows)
        assert re.search(case["error"], str(raised.value)), str(raised.value)
        return
    result = validate_cypher(case["query"], max_rows)
    if "equals" in case:
        assert result == case["equals"]
    if "startswith" in case:
        assert result.startswith(case["startswith"])
    if "endswith" in case:
        assert result.endswith(case["endswith"])


@pytest.mark.parametrize("case", CASES, ids=[str(i) for i in range(len(CASES))])
def test_the_python_validator_passes_every_shared_case(case: dict) -> None:
    check_case(case)


def test_every_case_has_exactly_one_kind_of_expectation() -> None:
    for case in CASES:
        kinds = {"equals", "startswith", "endswith", "ok", "error"} & case.keys()
        assert kinds and ("error" not in kinds or len(kinds) == 1), case

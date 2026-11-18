"""The browser's code, run with Node (spec 13a).

- The read-only check's JavaScript port (`site/cypher-check.js`) against the cases the
  Python validator passes (tests/fixtures/cypher_cases.json).
- The page's database code (`site/graph-db.js`) with the real Relate WebAssembly build
  and `site/data/graph.json`: load, search, a path, every prepared question against
  Neo4j's stored rows, a refused write, the row cap (tests/js/graph_check.mjs).

Node runs the code the browser runs, minus the DOM and the Worker. It is on every
GitHub runner; locally the tests are skipped without it. The WebAssembly check needs
the vendored build (`python scripts/vendor_relate.py`) and is skipped without it; the
Pages workflow runs it after vendoring, before it uploads the site.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
NODE = shutil.which("node")


def run_node(*args: str) -> dict:
    if NODE is None:
        if os.environ.get("CI"):
            pytest.fail("Node is missing on CI")
        pytest.skip("Node is not installed")
    result = subprocess.run(
        [NODE, *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=300
    )
    lines = result.stdout.strip().splitlines()
    report = json.loads(lines[-1]) if lines else {}
    assert result.returncode == 0, report or result.stderr[-4000:]
    return report


def test_the_javascript_validator_passes_every_shared_case() -> None:
    report = run_node("tests/js/cypher_cases.mjs", str(SITE), "tests/fixtures/cypher_cases.json")
    cases = json.loads((ROOT / "tests/fixtures/cypher_cases.json").read_text(encoding="utf-8"))
    assert report["passed"] == len(cases["validate"]) + len(cases["bind_times"])


def test_the_page_database_answers_like_neo4j() -> None:
    if not (SITE / "vendor" / "relate" / "relate_bg.wasm").exists():
        pytest.skip("Relate's WebAssembly build is not vendored (python scripts/vendor_relate.py)")
    report = run_node("tests/js/graph_check.mjs", str(SITE))
    assert report["failures"] == []
    assert report["nodes"] == 1761 and report["relationships"] == 2534
    assert set(report["verdicts"].values()) <= {"identical", "same rows", "tie"}

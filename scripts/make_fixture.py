"""Cut the test fixture `tests/fixtures/stream_sample.jsonl` from a real recording.

Usage: uv run python scripts/make_fixture.py data/recordings/<recording>.jsonl

Keeps a deterministic mix of about 300 raw messages in their original order: messages
the consumer keeps, plus messages it must filter out (other wikis, other namespaces,
other change types, canary events). Lines are copied unchanged.
"""

import argparse
import json
import sys
from pathlib import Path

from wikipulse.stream.filters import is_canary, is_wanted

FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "stream_sample.jsonl"

# How many messages of each kind to keep (the first ones found, in stream order).
QUOTAS = {
    "wanted": 150,
    "canary": 10,
    "enwiki_other_namespace": 40,
    "enwiki_other_type": 30,
    "other_wiki": 70,
}


def category(raw: dict) -> str:
    """Which quota a raw message counts against."""
    if is_canary(raw):
        return "canary"
    if is_wanted(raw):
        return "wanted"
    if raw.get("wiki") != "enwiki":
        return "other_wiki"
    if raw.get("namespace") != 0:
        return "enwiki_other_namespace"
    return "enwiki_other_type"


def select(lines: list[str]) -> list[str]:
    taken = dict.fromkeys(QUOTAS, 0)
    kept: list[str] = []
    for line in lines:
        kind = category(json.loads(json.loads(line)["data"]))
        if taken[kind] < QUOTAS[kind]:
            taken[kind] += 1
            kept.append(line)
    print(f"Selected {len(kept)} messages: {taken}")
    return kept


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("recording", type=Path)
    args = parser.parse_args()
    lines = args.recording.read_text(encoding="utf-8").splitlines()
    FIXTURE.write_text("\n".join(select(lines)) + "\n", encoding="utf-8", newline="\n")
    print(f"Wrote {FIXTURE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

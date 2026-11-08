"""Ask the knowledge graph: `uv run python -m wikipulse.qa.ask "question"`.

Prints the answer with numbered citations (page title, revision and a permanent link
to that revision) and the facts it used. Options:

- `--show-context`: also every retrieved fact, the generated Cypher and search notes;
- `--channels graph,cypher,vector`: which retrieval channels to use (default: all);
- `--as-of 2026-11-05T23:00:00+00:00`: "now" for "today" and "this week" (default: the
  newest event in the graph, i.e. event time);
- `--model ollama:qwen3:4b`: another model than QA_MODEL;
- `--json`: the whole answer (with its context) as JSON.

Needs the search indexes: `uv run python -m wikipulse.qa.index` once, and again after
the graph changed.
"""

import argparse
import io
import sys
from datetime import datetime

from wikipulse.config import get_settings
from wikipulse.logging_setup import setup_logging
from wikipulse.qa.answer import QASession
from wikipulse.qa.retrieve import ALL_CHANNELS
from wikipulse.qa.types import Answer, Channel


def parse_channels(text: str) -> list[Channel]:
    names = [part.strip() for part in text.split(",") if part.strip()]
    unknown = [name for name in names if name not in ALL_CHANNELS]
    if unknown or not names:
        raise argparse.ArgumentTypeError(f"channels are {', '.join(sorted(ALL_CHANNELS))}")
    return names  # type: ignore[return-value]


def parse_time(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise argparse.ArgumentTypeError("give a time zone, e.g. 2026-11-05T23:00:00+00:00")
    return moment


def render(result: Answer, show_context: bool = False) -> str:
    lines = [result.text, ""]
    if result.citations:
        lines.append("Sources:")
        for number, citation in enumerate(result.citations, start=1):
            lines.append(f"  [{number}] {citation.label()}  {citation.url}")
        lines.append("")
    if result.facts:
        lines.append("Facts used:")
        lines += [f"  {fact.id} ({fact.channel}) {fact.text}" for fact in result.facts]
        lines.append("")
    if result.uncited_sentences:
        lines.append("Warning: sentences without a cited fact:")
        lines += [f"  - {sentence}" for sentence in result.uncited_sentences]
        lines.append("")
    context = result.context
    if context is not None:
        counts = ", ".join(f"{k} {v}" for k, v in context.channel_counts().items())
        seconds = ", ".join(f"{k} {v:.1f} s" for k, v in result.seconds.items())
        lines.append(
            f"Retrieved {len(context.facts)} facts ({counts}); {seconds}; "
            f"model {result.model or 'none'}; as of {context.as_of}."
        )
        if show_context:
            lines += ["", "All retrieved facts:"]
            for fact in context.facts:
                sources = "; ".join(c.label() for c in fact.sources) or "no source"
                lines.append(f"  {fact.id} ({fact.channel}) {fact.text}  <{sources}>")
            steps = ", ".join(f"{k} {v:.1f} s" for k, v in context.seconds.items())
            lines += ["", f"Retrieval steps: {steps}"]
            lines += ["", f"Cypher ({context.cypher_rows} rows):", f"{context.cypher}"]
            if context.entities:
                names = ", ".join(f"{e.name} ({e.key})" for e in context.entities)
                lines.append(f"Entities named: {names}")
            for note in context.notes:
                lines.append(f"Note: {note}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("question")
    parser.add_argument("--channels", type=parse_channels, default=sorted(ALL_CHANNELS))
    parser.add_argument("--as-of", type=parse_time, default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--show-context", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    setup_logging()
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    settings = get_settings()
    with QASession.from_settings(settings, channels=args.channels, model=args.model) as session:
        result = session.ask(args.question, as_of=args.as_of)
    if args.json:
        print(result.model_dump_json(indent=2))
    else:
        print(render(result, show_context=args.show_context))
    return 0


if __name__ == "__main__":
    sys.exit(main())

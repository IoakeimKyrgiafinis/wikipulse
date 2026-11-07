"""Label a random sample of spikes as `news`, `not news` or `unsure`.

    uv run python -m wikipulse.eval.label --run-id loose --from 2026-11-06 --to 2026-11-07
    uv run python -m wikipulse.eval.label --run-id loose --from 2026-11-06 --to 2026-11-07 --manual

The sample (default 50 spikes) is drawn with a fixed seed, so the LLM and a human label
the same spikes. For each spike the labeler sees the page summary (Wikipedia REST API)
and the edit comments of the burst (MediaWiki API), both cached in `data/cache/wikipedia/`.

- Default: an LLM labels (`EVAL_LABEL_MODEL`, e.g. `ollama:qwen3:8b`, through
  `wikipulse.llm`), with temperature 0 and a JSON-schema answer. It skips spikes that
  already have a human label or a label from the same model.
- `--manual`: a terminal tool for a human. It skips spikes a human already labeled, and
  shows the LLM's label only *after* you answer, so it can't anchor you.

Labels are appended to `evals/labels/spike_labels.jsonl`; nothing is ever overwritten.
"""

import argparse
import json
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import psycopg

from wikipulse.config import Settings, get_settings
from wikipulse.db.spikes import spikes_for_run
from wikipulse.detect.detector import SpikeRecord
from wikipulse.eval.label_store import (
    DEFAULT_LABELS_PATH,
    LabelRecord,
    append_record,
    load_records,
    record_for,
)
from wikipulse.eval.metrics import (
    DEFAULT_SAMPLE_SIZE,
    DEFAULT_SEED,
    LABELS,
    day_start,
    sample_spikes,
    spike_end,
)
from wikipulse.eval.wiki_client import WikiClient
from wikipulse.llm import LLMClient, get_client
from wikipulse.logging_setup import setup_logging

logger = logging.getLogger(__name__)

MAX_COMMENTS = 30  # edit comments shown per spike
MAX_EXTRACT_CHARS = 1500

SYSTEM_PROMPT = """\
You judge bursts of editing on English Wikipedia. A detector noticed that several
people suddenly edited one article. Decide whether the burst is NEWS: driven by a
real-world event that happened, or became public, within a few days of the burst, and
that news media report on. Typical news: deaths, elections and vote counts, disasters,
attacks, arrests and trials, sports matches and results, transfers, film/TV/music
releases and episodes, announcements, scandals, awards.

People often tidy, source and reword an article *because* it is in the news, so
copyediting can still be part of a news burst. Signs of news: the summary mentions an
event dated within a few days of the burst (an election held, a match played, a death,
a release, an arrest), or the edit comments mention results, deaths, announcements or
what just happened.

NOT NEWS: no recent event behind the burst, for example vandalism and its reverts,
edit wars, routine maintenance or cleanup of an article with no current event, a class
or editathon project, one person's slow improvement of a topic, formatting, trivia.

UNSURE: the information is not enough to tell.

The date of the burst is "today". Your own knowledge may end before it, so an event
that looks like it is in the future to you may already have happened: trust the summary
and the edit comments over your memory.

Answer with JSON: {"label": "news" | "not news" | "unsure", "reason": "<one sentence>"}.
"""

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": list(LABELS)},
        "reason": {"type": "string"},
    },
    "required": ["label", "reason"],
}


# --- Context for the labeler --------------------------------------------------------


@dataclass
class SpikeContext:
    """What a labeler sees about one spike."""

    spike: SpikeRecord
    description: str = ""
    extract: str = ""
    comments: list[str] = field(default_factory=list)

    def as_text(self) -> str:
        s = self.spike
        comments = "\n".join(f"- {c}" for c in self.comments) or "- (no edit comments)"
        return (
            f"Today (the day of the burst): {s.first_edit_at:%A %d %B %Y}\n"
            f"Article: {s.title}\n"
            f"Short description: {self.description or '(none)'}\n"
            f"Summary: {self.extract or '(no summary available)'}\n\n"
            f"Burst: {s.total_edits} edits by {s.distinct_editors} different people "
            f"({s.revert_edits} reverts) from {s.first_edit_at:%Y-%m-%d %H:%M} to "
            f"{spike_end(s):%Y-%m-%d %H:%M} UTC.\n"
            f"Edit comments during the burst (newest first):\n{comments}"
        )


def summary_url(rest_url: str, title: str) -> str:
    return f"{rest_url}/page/summary/{quote(title.replace(' ', '_'), safe='')}"


def revisions_params(spike: SpikeRecord) -> dict[str, Any]:
    """MediaWiki API query for the revisions of the burst, newest first."""

    def mw_time(moment: datetime) -> str:
        return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "action": "query",
        "prop": "revisions",
        "titles": spike.title,
        "rvprop": "timestamp|comment",
        "rvstart": mw_time(spike_end(spike) + timedelta(minutes=1)),
        "rvend": mw_time(spike.first_edit_at - timedelta(minutes=1)),
        "rvdir": "older",
        "rvlimit": "50",
        "redirects": "1",
        "format": "json",
        "formatversion": "2",
    }


def parse_comments(body: dict[str, Any]) -> list[str]:
    """Non-empty edit comments from a `prop=revisions` response, newest first."""
    comments = []
    for page in body.get("query", {}).get("pages", []):
        for revision in page.get("revisions", []):
            comment = " ".join(str(revision.get("comment", "")).split())
            if comment:
                comments.append(comment[:200])
    return comments[:MAX_COMMENTS]


def fetch_context(client: WikiClient, settings: Settings, spike: SpikeRecord) -> SpikeContext:
    """Page summary and burst edit comments for one spike (cached)."""
    context = SpikeContext(spike)
    label = spike.title[:60]
    try:
        summary = client.get_json(
            summary_url(settings.wiki_rest_url, spike.title), label=f"summary-{label}"
        )
        context.description = summary.get("description", "")
        context.extract = summary.get("extract", "")[:MAX_EXTRACT_CHARS]
    except httpx.HTTPStatusError as error:
        logger.info("No summary for %s (HTTP %d)", spike.title, error.response.status_code)
    revisions = client.get_json(
        settings.wiki_api_url, revisions_params(spike), label=f"revisions-{label}"
    )
    context.comments = parse_comments(revisions)
    return context


# --- LLM labeler --------------------------------------------------------------------


def parse_answer(content: str) -> tuple[str, str]:
    """(label, reason) from the model's JSON answer text; ValueError if it isn't valid."""
    return answer_from_data(json.loads(content))


def answer_from_data(answer: Any) -> tuple[str, str]:
    """(label, reason) from the already parsed JSON answer; ValueError if it isn't valid."""
    if not isinstance(answer, dict):
        raise ValueError(f"answer is not a JSON object: {answer!r}")
    label = str(answer.get("label", "")).strip().lower()
    if label not in LABELS:
        raise ValueError(f"unknown label {label!r}")
    return label, str(answer.get("reason", "")).strip()


class LLMLabeler:
    """Asks an LLM for a label: thinking off, temperature 0, a JSON-schema answer.

    The client (`wikipulse.llm`) checks the answer against `ANSWER_SCHEMA` and asks once
    more if it isn't valid, then raises `StructuredOutputError` (a ValueError).
    """

    def __init__(self, client: LLMClient) -> None:
        self.client = client

    def close(self) -> None:
        self.client.close()

    def label(self, context: SpikeContext) -> tuple[str, str]:
        """(label, reason) for one spike."""
        response = self.client.chat(
            context.as_text(),
            system=SYSTEM_PROMPT,
            json_schema=ANSWER_SCHEMA,
            temperature=0,
            think=False,
        )
        # The client already parsed and checked the JSON (a code fence included).
        return answer_from_data(response.data)


# --- Which spikes still need a label ------------------------------------------------


def needs_llm_label(spike: SpikeRecord, records: Sequence[LabelRecord], model: str) -> bool:
    """False if a human labeled it, or the same model already did."""
    for record in records:
        if record.is_for(spike) and (record.labeler == "human" or record.model == model):
            return False
    return True


def needs_human_label(spike: SpikeRecord, records: Sequence[LabelRecord]) -> bool:
    return not any(r.is_for(spike) and r.labeler == "human" for r in records)


def llm_label_of(spike: SpikeRecord, records: Sequence[LabelRecord]) -> str | None:
    labels = [r.label for r in records if r.is_for(spike) and r.labeler == "llm"]
    return labels[-1] if labels else None


def label_with_llm(
    sample: Sequence[SpikeRecord],
    contexts: Callable[[SpikeRecord], SpikeContext],
    labeler: LLMLabeler,
    model_setting: str,
    run_id: str,
    path: Path,
) -> int:
    """Label every spike of the sample that needs it; returns how many were labeled."""
    records = load_records(path)
    done = 0
    for number, spike in enumerate(sample, start=1):
        if not needs_llm_label(spike, records, model_setting):
            logger.info("[%d/%d] %s: already labeled", number, len(sample), spike.title)
            continue
        label, reason = labeler.label(contexts(spike))
        record = record_for(
            spike, run_id, label, "llm", datetime.now(UTC), model=model_setting, reason=reason
        )
        append_record(path, record)
        records.append(record)
        done += 1
        logger.info("[%d/%d] %s: %s (%s)", number, len(sample), spike.title, label, reason)
    return done


# --- Manual labeling ----------------------------------------------------------------

KEYS = {"n": "news", "x": "not news", "u": "unsure"}


def label_manually(
    sample: Sequence[SpikeRecord],
    contexts: Callable[[SpikeRecord], SpikeContext],
    run_id: str,
    path: Path,
    ask: Callable[[str], str] = input,
    show: Callable[[str], None] = print,
) -> tuple[int, int, int]:
    """Terminal labeling. Returns (labeled, compared with an LLM label, agreements)."""
    records = load_records(path)
    todo = [spike for spike in sample if needs_human_label(spike, records)]
    show(f"{len(todo)} of {len(sample)} sampled spikes have no human label yet.")
    labeled = compared = agreed = 0
    for number, spike in enumerate(todo, start=1):
        show("\n" + "=" * 78 + f"\n[{number}/{len(todo)}]")
        show(contexts(spike).as_text())
        answer = ""
        while answer not in {*KEYS, "s", "q"}:
            answer = ask("news (n), not news (x), unsure (u), skip (s), quit (q)? ").strip()
        if answer == "q":
            break
        if answer == "s":
            continue
        note = ask("Optional note (why? what made it unclear?): ").strip()
        record = record_for(spike, run_id, KEYS[answer], "human", datetime.now(UTC), reason=note)
        append_record(path, record)
        labeled += 1
        llm = llm_label_of(spike, records)
        if llm is not None:
            compared += 1
            agreed += llm == KEYS[answer]
            show(f"You: {KEYS[answer]}. The LLM said: {llm}.")
        records.append(record)
    if compared:
        show(f"\nYou agreed with the LLM on {agreed} of {compared} spikes.")
    return labeled, compared, agreed


# --- Command line -------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True, help="detector run to sample spikes from")
    parser.add_argument("--from", dest="date_from", type=date.fromisoformat, required=True)
    parser.add_argument("--to", dest="date_to", type=date.fromisoformat, required=True)
    parser.add_argument("--size", type=int, default=DEFAULT_SAMPLE_SIZE, help="sample size")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--manual", action="store_true", help="label yourself in the terminal")
    parser.add_argument("--model", help="LLM as backend:model (default: EVAL_LABEL_MODEL)")
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS_PATH)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    setup_logging()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    with psycopg.connect(settings.postgres_dsn) as conn:
        spikes = spikes_for_run(
            conn,
            args.run_id,
            day_start(args.date_from),
            day_start(args.date_to) + timedelta(days=1),
        )
    sample = sample_spikes(spikes, args.size, args.seed)
    logger.info("Sampled %d of %d spikes of run %s", len(sample), len(spikes), args.run_id)
    cache_dir = settings.data_dir / "cache" / "wikipedia"
    with WikiClient(settings.user_agent, cache_dir, settings.wiki_api_min_interval_s) as client:

        def contexts(spike: SpikeRecord) -> SpikeContext:
            return fetch_context(client, settings, spike)

        if args.manual:
            label_manually(sample, contexts, args.run_id, args.labels)
        else:
            model_setting = args.model or settings.eval_label_model
            labeler = LLMLabeler(get_client(model_setting, settings))
            try:
                done = label_with_llm(
                    sample, contexts, labeler, model_setting, args.run_id, args.labels
                )
            finally:
                labeler.close()
            logger.info("Labeled %d spikes with %s", done, model_setting)
        logger.info("Wikimedia API: %d requests, %d from cache", client.requests, client.cache_hits)
    return 0


if __name__ == "__main__":
    sys.exit(main())

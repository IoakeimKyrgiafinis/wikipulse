"""Evaluate knowledge graph extraction against Wikidata and labeled relationships.

    uv run python -m wikipulse.eval.extraction <command> --run-id 03b-default-fix
        --events 2026-11-05 --name 04c-v1 [options]

    report   evaluate and write evals/results/<date>_<name>.md and .json
    label    have the LLM judge (EVAL_LABEL_MODEL) label the 50 sampled relations;
             --manual to label them yourself
    extract  re-extract the events with --prompt / --model into
             data/extractions/<name>.jsonl (the graph is not touched); `report` and
             `label` read it with --from <name>
    compare  `compare 04c-v1 04c-v2`: two reports side by side

`--events` takes event numbers (`341,356`), a UTC day (`2026-11-05`) or a range of start
times (`2026-11-05T16:00:00+00:00..2026-11-05T19:00:00+00:00`).

Three measurements, each about a different failure:

1. **Linking accuracy.** For every name the model used whose text is exactly a Wikidata
   label (`wikidata.items_by_label`): if exactly one item carries that label, the linked
   ID must be that item. Names many items share ("Georgia") have no single answer; for
   them the report only says whether the link went to one of those items.
2. **Relation support.** Of the relationships whose two entities are both linked, how
   many Wikidata also states, with any property in either direction. A *lower bound* on
   correctness: Wikidata lags behind breaking news, and much of what an article says
   is never a Wikidata statement.
3. **Labeled precision.** A fixed sample of 50 relationships is judged against its
   source text (`label`, by an LLM or with `--manual` by you): correct, wrong, unsure.
   Precision = correct / (correct + wrong), with who labeled it.

Everything is broken down by entity type, and the report keeps the extraction's model
and prompt version in its configuration snapshot (`extraction_report.py` computes and
renders; this module fetches and runs).
"""

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

from wikipulse.config import Settings, get_settings
from wikipulse.db.graph import load_event, pending_events
from wikipulse.enrich.cache import PostgresCache
from wikipulse.enrich.client import WikimediaClient
from wikipulse.eval.detection import find_report, git_commit
from wikipulse.eval.extraction_data import (
    DEFAULT_EXTRACTIONS_DIR,
    EventSelection,
    ExtractionData,
    append_event,
    load_from_file,
    load_from_graph,
    parse_events,
    read_records,
)
from wikipulse.eval.extraction_report import (
    build_report,
    check_links,
    check_support,
    compare_reports,
    link_error_ids,
    linked_pairs,
    render_markdown,
    summary_line,
)
from wikipulse.eval.relation_labels import (
    DEFAULT_RELATION_LABELS_PATH,
    DEFAULT_SAMPLE_SIZE,
    DEFAULT_SEED,
    JUDGE_PROMPT_VERSION,
    LLMRelationJudge,
    RelationToLabel,
    SourceText,
    label_manually,
    label_with_llm,
    labels_by_relation,
    load_relation_records,
    sample_relations,
)
from wikipulse.eval.wikidata import EXCLUDED_CLASSES, LABEL_LANGUAGES, Wikidata, describe
from wikipulse.graph.extract import PROMPT_VERSION, Extractor
from wikipulse.graph.inputs import EventInput, fetch_changes, trim, with_changes
from wikipulse.graph.link import Linker
from wikipulse.graph.run import EVENT_ERRORS, wiki_client
from wikipulse.graph.store import GraphEvent, GraphStore, build_graph_event
from wikipulse.llm import get_client
from wikipulse.logging_setup import setup_logging

logger = logging.getLogger(__name__)

DEFAULT_RESULTS_DIR = Path("evals") / "results"
# Ground-truth answers are kept for 30 days: re-running a report gives the same numbers,
# instead of following Wikidata's edits from one day to the next.
EVAL_CACHE_TTL = timedelta(days=30)
GRAPH = "graph"


def eval_client(settings: Settings, cache_conn: psycopg.Connection) -> WikimediaClient:
    """One request at a time, at most one per WIKI_API_MIN_INTERVAL_S, cached for 30 days."""
    return WikimediaClient(
        settings.user_agent,
        PostgresCache(cache_conn),
        concurrency=1,
        ttl=EVAL_CACHE_TTL,
        min_interval_s=settings.wiki_api_min_interval_s,
    )


def event_number(event_key: str) -> int:
    return int(event_key.rpartition(":")[2])


# --- Loading ------------------------------------------------------------------------


def load_data(args: argparse.Namespace, settings: Settings) -> ExtractionData:
    """The extraction to evaluate: the graph, or a saved re-extraction (`--from`)."""
    if args.source == GRAPH:
        with GraphStore.from_settings(settings) as store:
            data = load_from_graph(store.driver, args.run_id, args.events)
    else:
        path = args.extractions_dir / f"{args.source}.jsonl"
        if not path.exists():
            raise SystemExit(f"No saved extraction {path}; run the `extract` command first")
        data = load_from_file(path, args.run_id, args.events)
    if not data.events:
        raise SystemExit(f"No extracted events of run {args.run_id!r}: {args.events.describe()}")
    logger.info(
        "%d events, %d entities, %d relations from %s",
        len(data.events),
        len(data.entities),
        len(data.relations),
        data.source,
    )
    return data


async def source_texts(
    conn: psycopg.Connection,
    wiki: WikimediaClient,
    settings: Settings,
    run_id: str,
    keys: Sequence[tuple[str, str]],
) -> dict[tuple[str, str], SourceText | None]:
    """(event key, page title) -> the page as the extraction model saw it."""
    texts: dict[tuple[str, str], SourceText | None] = {}
    events: dict[str, EventInput | None] = {}
    for event_key, title in keys:
        if event_key not in events:
            events[event_key] = load_event(conn, run_id, event_number(event_key))
        event = events[event_key]
        page = next((p for p in event.pages if p.title == title), None) if event else None
        if page is None:
            logger.warning("Page %r of event %s not found in Postgres", title, event_key)
            texts[(event_key, title)] = None
            continue
        page = await fetch_changes(wiki, settings.wiki_api_url, page, settings.extract_max_chars)
        summary = trim(page.summary, settings.extract_max_chars) if page.summary else None
        texts[(event_key, title)] = SourceText(page.title, page.description, summary, page.changes)
    return texts


def items_to_label(
    data: ExtractionData,
    sample: Sequence[Any],
    texts: dict[tuple[str, str], SourceText | None],
) -> list[RelationToLabel]:
    items = []
    for relation in sample:
        subject, object_ = data.entity(relation.subject), data.entity(relation.object)
        items.append(
            RelationToLabel(
                relation,
                subject.name,
                object_.name,
                subject.type,
                object_.type,
                texts.get((relation.event_key, relation.source_title)),
            )
        )
    return items


def config_snapshot(
    args: argparse.Namespace, settings: Settings, data: ExtractionData
) -> dict[str, Any]:
    return {
        "extraction": {
            "source": data.source,
            "models": data.models,
            "prompt_versions": data.prompt_versions,
            "extract_max_pages": settings.extract_max_pages,
            "extract_max_chars": settings.extract_max_chars,
        },
        "labels": {
            "judge_model": args.model or settings.eval_label_model,
            "judge_prompt_version": JUDGE_PROMPT_VERSION,
            "sample_size": args.sample,
            "seed": args.seed,
            "labels_file": args.labels.as_posix(),
        },
        "wikidata": {
            "sparql_url": settings.wikidata_sparql_url,
            "label_languages": list(LABEL_LANGUAGES),
            "excluded_classes": EXCLUDED_CLASSES,
            "cache_ttl_days": EVAL_CACHE_TTL.days,
        },
    }


# --- Commands -----------------------------------------------------------------------


async def run_report(args: argparse.Namespace, settings: Settings) -> Path:
    data = load_data(args, settings)
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as cache_conn:
        async with eval_client(settings, cache_conn) as wiki:
            wikidata = Wikidata(wiki, settings.wikidata_sparql_url)
            names = [name for entity in data.entities.values() for name in entity.aliases]
            items_by_label = await wikidata.items_by_label(names)
            statements = await wikidata.statements_between(linked_pairs(data))
            link_checks = check_links(data, items_by_label)
            descriptions = await describe(wikidata, link_error_ids(link_checks))
            stats = wiki.stats
    logger.info("Wikidata: %d requests, %d cache hits", stats.requests, stats.cache_hits)
    sample = sample_relations(data.relations, args.sample, args.seed)
    labels = labels_by_relation(load_relation_records(args.labels))
    now = datetime.now(UTC)  # report metadata only
    report = build_report(
        args.name,
        data,
        args.events.describe(),
        link_checks,
        check_support(data, statements),
        sample,
        labels,
        config_snapshot(args, settings, data),
        now.isoformat(),
        git_commit(),
        args.note,
        descriptions,
    )
    args.results_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{now:%Y-%m-%d}_{args.name}"
    (args.results_dir / f"{stem}.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    md_path = args.results_dir / f"{stem}.md"
    md_path.write_text(render_markdown(report), encoding="utf-8", newline="\n")
    print(summary_line(report))
    return md_path


async def run_label(args: argparse.Namespace, settings: Settings) -> None:
    data = load_data(args, settings)
    sample = sample_relations(data.relations, args.sample, args.seed)
    keys = sorted({(r.event_key, r.source_title) for r in sample})
    with (
        psycopg.connect(settings.postgres_dsn, autocommit=True) as conn,
        psycopg.connect(settings.postgres_dsn, autocommit=True) as cache_conn,
    ):
        async with eval_client(settings, cache_conn) as wiki:
            texts = await source_texts(conn, wiki, settings, args.run_id, keys)
    items = items_to_label(data, sample, texts)

    def now() -> datetime:
        return datetime.now(UTC)  # label metadata only

    if args.manual:
        label_manually(items, args.name, args.labels, now)
        return
    model = args.model or settings.eval_label_model
    judge = LLMRelationJudge(get_client(model, settings))
    try:
        done = await asyncio.to_thread(
            label_with_llm, items, judge, model, args.name, args.labels, now
        )
    finally:
        judge.close()
    logger.info("Labeled %d of %d sampled relations with %s", done, len(items), model)


async def extract_event(
    event: EventInput,
    extractor: Extractor,
    linker: Linker,
    wiki: WikimediaClient,
    settings: Settings,
) -> GraphEvent:
    """The graph pipeline of `wikipulse.graph.run` (changes, LLM, links), not written."""
    max_pages = settings.extract_max_pages
    shown = await with_changes(
        wiki, settings.wiki_api_url, event.pages[:max_pages], settings.extract_max_chars
    )
    extraction = await asyncio.to_thread(extractor.extract, event, shown)
    types = {entity.name: entity.type for entity in extraction.entities}
    links = await linker.link(list(types), event.pages, types)
    pages = [*shown, *event.pages[max_pages:]]
    return build_graph_event(event, pages, extraction, links, datetime.now(UTC))


def selected_numbers(conn: psycopg.Connection, run_id: str, events: EventSelection) -> list[int]:
    """The run's current events in the selection, oldest first."""
    numbers = pending_events(conn, run_id, since=events.since, until=events.until, force=True)
    if events.numbers is not None:
        numbers = [n for n in numbers if n in events.numbers]
    return numbers


async def run_extract(args: argparse.Namespace, settings: Settings) -> str:
    path = args.extractions_dir / f"{args.name}.jsonl"
    done = set() if args.force else {r["number"] for r in read_records(path)}
    model = args.model or settings.extract_model
    failed = processed = 0
    with (
        psycopg.connect(settings.postgres_dsn, autocommit=True) as conn,
        psycopg.connect(settings.postgres_dsn, autocommit=True) as cache_conn,
        get_client(model, settings) as llm,
    ):
        numbers = [n for n in selected_numbers(conn, args.run_id, args.events) if n not in done]
        logger.info("%d events to extract into %s (%d done)", len(numbers), path, len(done))
        extractor = Extractor(llm, settings.extract_max_chars, args.prompt)
        async with wiki_client(settings, cache_conn) as wiki:
            linker = Linker(wiki, settings.wiki_api_url, settings.wikidata_api_url)
            for count, number in enumerate(numbers, start=1):
                event = load_event(conn, args.run_id, number)
                if event is None:
                    continue
                try:
                    graph_event = await extract_event(event, extractor, linker, wiki, settings)
                except EVENT_ERRORS as error:
                    failed += 1
                    logger.warning("Event %s failed: %r", event.key, error)
                    continue
                append_event(path, graph_event)
                processed += 1
                logger.info(
                    "[%d/%d] %s: %d entities, %d relations",
                    count,
                    len(numbers),
                    event.key,
                    len(graph_event.entities),
                    len(graph_event.relations),
                )
        usage = llm.total_usage
    return (
        f"Extracted {processed} events ({failed} failed) with {model}, prompt {args.prompt}, "
        f"into {path}. LLM: {usage.calls} calls, {usage.output_tokens} tokens out, "
        f"{usage.latency_s:.0f} s."
    )


def run_compare(args: argparse.Namespace) -> None:
    path_a = find_report(args.a, args.results_dir)
    path_b = find_report(args.b, args.results_dir)
    a = json.loads(path_a.read_text(encoding="utf-8"))
    b = json.loads(path_b.read_text(encoding="utf-8"))
    rows = [("measure", a["name"], b["name"]), *compare_reports(a, b)]
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)))


# --- Command line -------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("report", "label", "extract"):
        p = sub.add_parser(command)
        p.add_argument("--run-id", required=True, help="grouping run whose events to use")
        p.add_argument(
            "--events",
            type=parse_events,
            default=EventSelection(),
            help="341,356 | 2026-11-05 | <start>..<end> (default: all)",
        )
        p.add_argument("--name", required=True, help="name of this evaluation run")
        p.add_argument("--extractions-dir", type=Path, default=DEFAULT_EXTRACTIONS_DIR)
    for command in ("report", "label"):
        p = sub.choices[command]
        p.add_argument("--from", dest="source", default=GRAPH, help="graph or a saved extraction")
        p.add_argument("--sample", type=int, default=DEFAULT_SAMPLE_SIZE)
        p.add_argument("--seed", type=int, default=DEFAULT_SEED)
        p.add_argument("--labels", type=Path, default=DEFAULT_RELATION_LABELS_PATH)
        p.add_argument("--model", help="judge as backend:model (default: EVAL_LABEL_MODEL)")
    sub.choices["label"].add_argument("--manual", action="store_true", help="label yourself")
    report = sub.choices["report"]
    report.add_argument("--note", default="")
    report.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    extract = sub.choices["extract"]
    extract.add_argument("--prompt", default=PROMPT_VERSION, help="prompt version, e.g. extract_v2")
    extract.add_argument("--model", help="backend:model (default: EXTRACT_MODEL)")
    extract.add_argument("--force", action="store_true", help="extract saved events again")
    compare = sub.add_parser("compare")
    compare.add_argument("a", help="report name or JSON path")
    compare.add_argument("b", help="report name or JSON path")
    compare.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    return parser.parse_args(argv)


async def amain(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    setup_logging()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("neo4j").setLevel(logging.WARNING)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.command == "compare":
        run_compare(args)
    elif args.command == "report":
        print(f"Report: {await run_report(args, settings)}")
    elif args.command == "label":
        await run_label(args, settings)
    else:
        print(await run_extract(args, settings))
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(amain(argv))


if __name__ == "__main__":
    sys.exit(main())

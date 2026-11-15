"""Export one processed day from Postgres and Neo4j for the static demo in `site/`.

    uv run python scripts/export_demo.py
    uv run python scripts/export_demo.py --run-id 03b-default-fix --day 2026-11-05

Runs on the laptop, with the Docker services up; what it writes, `site/data/day.json`,
is committed and served by GitHub Pages (`.github/workflows/pages.yml`). The page
(`site/app.js`) replays the day in the browser: nothing runs on a server.

What it reads, all of it read-only:

- **Postgres**: the grouping run's events (spec 03b), their member spikes (02a) with
  start and end times, edit and editor *counts* and scores, and each page's canonical
  title, Wikidata id and short description (03a).
- **Neo4j**: for the events the graph extraction (04b) processed, the headline and
  description, the revision of each page the model read, the entities with their
  Wikidata ids, the relations, and the analyst's briefing (05d) where one exists.
- **The question-answering evaluation (05b)**: a few real answers, with the judges'
  scores, from `evals/results/<date>_05b-v1.json` and the cited revisions from
  `data/qa_eval/05b-v1/answers.jsonl`. Skipped if that file is missing.

Privacy: the export names every field it writes (the pydantic models below, with
`extra="forbid"`), and none of them holds an editor, an IP address, an edit summary or
a correlation id. `tests/test_demo_site.py` checks the committed file again.
"""

import argparse
import json
import logging
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # so `wikipulse` imports when run as a script

from wikipulse.config import get_settings  # noqa: E402
from wikipulse.logging_setup import setup_logging  # noqa: E402

logger = logging.getLogger("export_demo")

OUT_FILE = ROOT / "site" / "data" / "day.json"
QA_RESULTS = ROOT / "evals" / "results" / "2026-11-08_05b-v1.json"
QA_ANSWERS = ROOT / "data" / "qa_eval" / "05b-v1" / "answers.jsonl"
SCHEMA_VERSION = 1
WIKIPEDIA_PAGE = "https://en.wikipedia.org/wiki/"
SOURCES_HEADER = "\n\nSources:\n"
# Real answers of the 05b evaluation shown as prepared examples: six the judges scored
# fully correct and two they scored wrong, so the demo shows both sides.
ANSWER_IDS = ("q07", "q11", "q12", "q14", "q18", "q22", "q19", "q24")


def utc_text(moment: datetime) -> str:
    """A timestamp as compact ISO 8601 in UTC, to the second ("2026-11-05T16:02:35Z")."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


UtcTime = Annotated[datetime, PlainSerializer(utc_text, return_type=str)]


class Strict(BaseModel):
    """Every model refuses unknown fields: the file holds exactly what is declared."""

    model_config = ConfigDict(extra="forbid")


class DemoSpike(Strict):
    start: UtcTime = Field(description="When the detector raised the spike (event time).")
    end: UtcTime | None = Field(description="When it ended; None: still active at the end.")
    edits: int = Field(ge=0, description="Human edits during the spike.")
    editor_count: int = Field(ge=0, description="How many different people edited (a count).")
    score: float


class DemoPage(Strict):
    title: str
    url: str = Field(description="The page on English Wikipedia.")
    wikidata_id: str | None = None
    description: str | None = Field(None, description="Wikipedia's short description.")
    revision: int | None = Field(None, description="The revision the extraction model read.")
    spikes: list[DemoSpike]


class DemoEntity(Strict):
    name: str
    type: str | None = None
    wikidata_id: str | None = None
    source: str | None = Field(None, description="The page title it was read from.")
    revision: int | None = None


class DemoRelation(Strict):
    subject: str
    relation: str
    object: str
    source: str | None = None
    revision: int | None = None


class DemoSource(Strict):
    title: str
    url: str | None = None
    revision: int | None = None


class DemoBriefing(Strict):
    text: str
    sources: list[DemoSource]
    verified: bool | None = None
    model: str | None = None


class DemoEvent(Strict):
    number: int
    title: str = Field(description="Grouping's title: the strongest page.")
    headline: str | None = Field(None, description="The extraction model's headline.")
    description: str | None = None
    started_at: UtcTime
    ended_at: UtcTime | None
    score: float
    pages: list[DemoPage]
    entities: list[DemoEntity] = Field(default_factory=list)
    relations: list[DemoRelation] = Field(default_factory=list)
    briefing: DemoBriefing | None = None


class DemoAnswer(Strict):
    id: str
    question: str
    answer: str
    sources: list[DemoSource]
    notes: list[str] = Field(default_factory=list)
    model: str | None = None
    seconds: float | None = None
    correctness: dict[str, float] = Field(description="Judge model -> score 0 to 2.")


class DemoDay(Strict):
    schema_version: int
    day: date
    run_id: str
    spike_run_id: str
    start: UtcTime
    end: UtcTime
    graph_start: UtcTime | None = Field(description="First event start the graph covers.")
    graph_end: UtcTime | None
    extraction_model: str | None = None
    counts: dict[str, int]
    events: list[DemoEvent]
    answers: list[DemoAnswer] = Field(default_factory=list)


# --- Reading Postgres ---------------------------------------------------------------------
EVENTS_SQL = """
SELECT number, title, started_at, ended_at, score, spike_run_id
FROM events
WHERE run_id = %(run)s AND status <> 'merged'
  AND started_at >= %(start)s AND started_at < %(end)s
ORDER BY number
"""
# Only named columns: no correlation id, and the spikes table holds no editors anyway.
MEMBERS_SQL = """
SELECT ep.event_number, coalesce(p.canonical_title, s.title) AS title, p.wikidata_id,
       p.description, s.started_at, s.ended_at, s.total_edits, s.distinct_editors, s.score
FROM event_pages ep
JOIN spikes s ON s.id = ep.spike_id
LEFT JOIN pages p ON p.title = s.title
WHERE ep.run_id = %(run)s
ORDER BY ep.event_number, s.started_at, s.id
"""
MODEL_SQL = """
SELECT model, count(*) AS n FROM graph_extractions
WHERE run_id = %(run)s AND status = 'ok' GROUP BY model ORDER BY n DESC LIMIT 1
"""


def read_postgres(dsn: str, run_id: str, start: datetime, end: datetime) -> dict[str, Any]:
    """The run's events of the day, their member spikes, and the extraction model."""
    params = {"run": run_id, "start": start, "end": end}
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor(row_factory=dict_row) as cur:
        events = cur.execute(EVENTS_SQL, params).fetchall()
        members = cur.execute(MEMBERS_SQL, params).fetchall()
        model = cur.execute(MODEL_SQL, params).fetchone()
    return {"events": events, "members": members, "model": model["model"] if model else None}


# --- Reading Neo4j ------------------------------------------------------------------------
GRAPH_EVENTS = """
MATCH (ev:Event {run_id: $run})
OPTIONAL MATCH (p:Page)-[po:PART_OF]->(ev)
WITH ev, collect({title: p.title, revision: po.revision}) AS pages
RETURN ev.number AS number, ev.title AS headline, ev.description AS description,
       pages, properties(ev)['briefing'] AS briefing,
       coalesce(properties(ev)['briefing_sources'], []) AS briefing_sources,
       coalesce(properties(ev)['briefing_urls'], []) AS briefing_urls,
       properties(ev)['briefing_verified'] AS briefing_verified,
       properties(ev)['briefing_model'] AS briefing_model
"""
GRAPH_ENTITIES = """
MATCH (n:Entity)-[r:INVOLVED_IN]->(ev:Event {run_id: $run})
RETURN ev.number AS number, n.name AS name, coalesce(r.type, n.type) AS type,
       n.wikidata_id AS wikidata_id, r.source_title AS source,
       r.source_revision AS revision, r.confidence AS confidence
ORDER BY number, confidence DESC, name
"""
GRAPH_RELATIONS = """
MATCH (a:Entity)-[r:RELATED_TO]->(b:Entity)
WHERE r.event_key STARTS WITH $prefix
RETURN toInteger(substring(r.event_key, size($prefix))) AS number, a.name AS subject,
       r.relation AS relation, b.name AS object, r.source_title AS source,
       r.source_revision AS revision, r.confidence AS confidence
ORDER BY number, confidence DESC, subject, relation, object
"""


def read_graph(uri: str, user: str, password: str, run_id: str) -> dict[str, Any]:
    """Events, entities and relations of the run, in READ sessions."""
    from neo4j import READ_ACCESS, GraphDatabase

    with GraphDatabase.driver(uri, auth=(user, password)) as driver:
        with driver.session(default_access_mode=READ_ACCESS) as session:
            events = [r.data() for r in session.run(GRAPH_EVENTS, run=run_id)]
            entities = [r.data() for r in session.run(GRAPH_ENTITIES, run=run_id)]
            relations = [r.data() for r in session.run(GRAPH_RELATIONS, prefix=f"{run_id}:")]
    return {"events": events, "entities": entities, "relations": relations}


# --- Building the export (pure functions, tested without services) --------------------------
def page_url(title: str) -> str:
    """The English Wikipedia URL of a page title."""
    return WIKIPEDIA_PAGE + quote(title.replace(" ", "_"), safe="")


def build_pages(members: Iterable[Mapping[str, Any]], revisions: Mapping[str, int]) -> list:
    """One `DemoPage` per distinct page, in the order the pages joined the event."""
    pages: dict[str, DemoPage] = {}
    for row in members:
        title = row["title"]
        if title not in pages:
            pages[title] = DemoPage(
                title=title,
                url=page_url(title),
                wikidata_id=row.get("wikidata_id"),
                description=row.get("description"),
                revision=revisions.get(title),
                spikes=[],
            )
        pages[title].spikes.append(
            DemoSpike(
                start=row["started_at"],
                end=row["ended_at"],
                edits=row["total_edits"],
                editor_count=row["distinct_editors"],
                score=round(row["score"], 2),
            )
        )
    return list(pages.values())


def build_briefing(graph_event: Mapping[str, Any]) -> DemoBriefing | None:
    """The stored briefing split into its text and numbered sources (as the API does)."""
    text = graph_event.get("briefing")
    if not text:
        return None
    titles = list(graph_event.get("briefing_sources") or [])
    urls = list(graph_event.get("briefing_urls") or []) + [""] * len(titles)
    sources = []
    for title, url in zip(titles, urls, strict=False):
        revision = url.rpartition("oldid=")[2]
        sources.append(
            DemoSource(
                title=title,
                url=url or None,
                revision=int(revision) if revision.isdigit() else None,
            )
        )
    return DemoBriefing(
        text=text.split(SOURCES_HEADER, 1)[0].strip(),
        sources=sources,
        verified=graph_event.get("briefing_verified"),
        model=graph_event.get("briefing_model"),
    )


def by_number(rows: Iterable[Mapping[str, Any]]) -> dict[int, list[Mapping[str, Any]]]:
    """Rows grouped by their `number` (event number)."""
    grouped: dict[int, list[Mapping[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row.get("number", row.get("event_number")), []).append(row)
    return grouped


def build_event(
    row: Mapping[str, Any],
    members: Sequence[Mapping[str, Any]],
    graph_event: Mapping[str, Any] | None,
    entities: Sequence[Mapping[str, Any]],
    relations: Sequence[Mapping[str, Any]],
) -> DemoEvent:
    """One event: Postgres' pages and times, with the graph's view when it has one."""
    graph_event = graph_event or {}
    revisions = {p["title"]: p["revision"] for p in graph_event.get("pages", []) if p["title"]}
    return DemoEvent(
        number=row["number"],
        title=row["title"],
        headline=graph_event.get("headline"),
        description=graph_event.get("description"),
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        score=round(row["score"], 2),
        pages=build_pages(members, revisions),
        entities=[
            DemoEntity(
                name=e["name"],
                type=e.get("type"),
                wikidata_id=e.get("wikidata_id"),
                source=e.get("source"),
                revision=e.get("revision"),
            )
            for e in entities
        ],
        relations=[
            DemoRelation(
                subject=r["subject"],
                relation=r["relation"],
                object=r["object"],
                source=r.get("source"),
                revision=r.get("revision"),
            )
            for r in relations
        ],
        briefing=build_briefing(graph_event),
    )


def parse_citation(text: str) -> DemoSource:
    """'Ueno Bank, revision 1383953174' -> a source with a permanent link."""
    title, sep, revision = text.rpartition(", revision ")
    if sep and revision.isdigit():
        url = f"https://en.wikipedia.org/w/index.php?oldid={revision}"
        return DemoSource(title=title, url=url, revision=int(revision))
    return DemoSource(title=text)


def build_answers(
    results: Mapping[str, Any], answers: Iterable[Mapping[str, Any]], ids: Sequence[str]
) -> list[DemoAnswer]:
    """The chosen questions of the 05b report, with their answers' cited revisions."""
    scores = {q["id"]: q.get("correctness") or {} for q in results["questions"]}
    chosen = {a["question_id"]: a for a in answers if a["question_id"] in ids}
    out = []
    for qid in ids:
        answer = chosen.get(qid)
        if answer is None:
            logger.warning("Answer %s is not in the answers file; left out", qid)
            continue
        out.append(
            DemoAnswer(
                id=qid,
                question=answer["question"],
                answer=answer["text"],
                sources=[parse_citation(c) for c in answer.get("citations", [])],
                notes=list(answer.get("notes", [])),
                model=answer.get("model"),
                seconds=(answer.get("seconds") or {}).get("total"),
                correctness={k: float(v) for k, v in scores.get(qid, {}).items()},
            )
        )
    return out


def build_day(
    day: date,
    run_id: str,
    pg: Mapping[str, Any],
    graph: Mapping[str, Any],
    answers: list[DemoAnswer],
) -> DemoDay:
    """The whole export from what was read."""
    start = datetime.combine(day, time(), tzinfo=UTC)
    members = by_number(pg["members"])
    graph_events = {g["number"]: g for g in graph["events"]}
    entities, relations = by_number(graph["entities"]), by_number(graph["relations"])
    events = [
        build_event(
            row,
            members.get(row["number"], []),
            graph_events.get(row["number"]),
            entities.get(row["number"], []),
            relations.get(row["number"], []),
        )
        for row in pg["events"]
        if members.get(row["number"])
    ]
    in_graph = [e for e in events if e.headline]
    spike_runs = {row["spike_run_id"] for row in pg["events"]}
    return DemoDay(
        schema_version=SCHEMA_VERSION,
        day=day,
        run_id=run_id,
        spike_run_id=", ".join(sorted(spike_runs)),
        start=start,
        end=start + timedelta(days=1),
        graph_start=min((e.started_at for e in in_graph), default=None),
        graph_end=max((e.started_at for e in in_graph), default=None),
        extraction_model=pg.get("model"),
        counts={
            "events": len(events),
            "spikes": sum(len(p.spikes) for e in events for p in e.pages),
            "events_in_graph": len(in_graph),
            "entities": sum(len(e.entities) for e in events),
            "relations": sum(len(e.relations) for e in events),
            "briefings": sum(1 for e in events if e.briefing),
        },
        events=events,
        answers=answers,
    )


def to_json(export: DemoDay) -> str:
    """Compact, stable JSON (UTF-8 text, no extra whitespace)."""
    data = export.model_dump(mode="json")
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n"


def read_answers(ids: Sequence[str]) -> list[DemoAnswer]:
    """The prepared answers, or none if the 05b files are not on this machine."""
    if not QA_RESULTS.exists() or not QA_ANSWERS.exists():
        logger.warning("No 05b answers found (%s); exporting none", QA_ANSWERS)
        return []
    results = json.loads(QA_RESULTS.read_text(encoding="utf-8"))
    lines = QA_ANSWERS.read_text(encoding="utf-8").splitlines()
    return build_answers(results, (json.loads(line) for line in lines if line.strip()), ids)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", default="03b-default-fix", help="Grouping run to export.")
    parser.add_argument("--day", default="2026-11-05", help="The day (UTC), YYYY-MM-DD.")
    parser.add_argument("--out", type=Path, default=OUT_FILE)
    parser.add_argument("--no-answers", action="store_true", help="Leave out the 05b answers.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Read the stores, build the export, write it."""
    args = parse_args(argv)
    setup_logging()
    settings = get_settings()
    day = date.fromisoformat(args.day)
    start = datetime.combine(day, time(), tzinfo=UTC)
    pg = read_postgres(settings.postgres_dsn, args.run_id, start, start + timedelta(days=1))
    if not pg["events"]:
        logger.error("Run %s has no events on %s", args.run_id, day)
        return 1
    password = settings.neo4j_password.get_secret_value()
    graph = read_graph(settings.neo4j_uri, settings.neo4j_user, password, args.run_id)
    answers = [] if args.no_answers else read_answers(ANSWER_IDS)
    export = build_day(day, args.run_id, pg, graph, answers)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    text = to_json(export)
    args.out.write_text(text, encoding="utf-8", newline="\n")
    logger.info(
        "Wrote %s (%.0f KB): %s, %d answers",
        args.out,
        len(text.encode("utf-8")) / 1024,
        export.counts,
        len(export.answers),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

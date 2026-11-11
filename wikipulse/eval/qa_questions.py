"""The question set of the QA evaluation: `evals/labels/qa_questions.jsonl`.

    uv run python -m wikipulse.eval.qa_questions [--force]

Generates 25 questions from the frozen graph snapshot (`EVAL_QA_NEO4J_URI`, see
`scripts/snapshot_graph.py`), five of each type of spec 05a:

- `time`: "What happened today?" and other periods;
- `event`: "Who is involved in event X?", X named by title, number or page;
- `connection`: "How are A and B connected?";
- `aggregation`: counts, maxima and "more than one event" lists;
- `unanswerable`: questions the graph has no facts for.

The subjects (which event, which pair of entities) are picked by deterministic queries,
and so are the expected answers: the same snapshot always gives the same file. Each
record says how its answer was derived (`derived_by`). Unanswerable questions are
checked: the generator fails if the snapshot does mention what they ask about.

The file is in `evals/labels/`, so every record names its labeler. Generated questions
are `source: synthetic` with labeler `llm` (the generator was written by a coding agent
and its choices were not checked by a person). Questions a human writes or corrects
are `source: human`, `labeler: human`; they are never overwritten: `--force` replaces
only the synthetic records.
"""

import argparse
import json
import logging
import re
import sys
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import neo4j
from neo4j import GraphDatabase
from pydantic import BaseModel, Field

from wikipulse.config import Settings, get_settings
from wikipulse.logging_setup import setup_logging

logger = logging.getLogger(__name__)

DEFAULT_QUESTIONS_PATH = Path("evals") / "labels" / "qa_questions.jsonl"
QUESTION_TYPES = ("time", "event", "connection", "aggregation", "unanswerable")
QuestionType = Literal["time", "event", "connection", "aggregation", "unanswerable"]
# Events the 05a acceptance questions were tuned on; event and connection questions
# avoid them, so the set doesn't reward what was tuned by hand.
TUNED_EVENT_NUMBERS = (341, 496)
MAX_EXPECTED = 15  # expected entities or events listed per question


class QAQuestion(BaseModel):
    """One line of the questions file."""

    id: str
    question: str
    type: QuestionType
    expected_answer: str
    expected_entities: list[str] = Field(default_factory=list)  # Wikidata IDs
    expected_events: list[str] = Field(default_factory=list)  # event keys
    answerable: bool
    source: Literal["synthetic", "human"]
    labeler: Literal["llm", "human"]
    snapshot: str = ""  # the snapshot the expected answer was read from
    derived_by: str = ""  # how the expected answer was found


def load_questions(path: Path) -> list[QAQuestion]:
    """The questions of a questions file (none if it doesn't exist)."""
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [QAQuestion.model_validate_json(line) for line in lines if line.strip()]


def write_questions(path: Path, questions: Sequence[QAQuestion]) -> None:
    """Write the questions, one JSON object per line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(q.model_dump(mode="json"), ensure_ascii=False) for q in questions]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def merge_questions(
    existing: Sequence[QAQuestion], generated: Sequence[QAQuestion]
) -> list[QAQuestion]:
    """Human questions stay as they are; a generated question whose id a human record
    already uses is dropped."""
    humans = [q for q in existing if q.source == "human" or q.labeler == "human"]
    taken = {q.id for q in humans}
    return [*humans, *(q for q in generated if q.id not in taken)]


# --- Text helpers ---------------------------------------------------------------------


def fold(text: str) -> str:
    """Lower case without accents: "Peña" -> "pena"."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def mentions(text: str, term: str) -> bool:
    """Whether `term` occurs in `text` as whole words (case and accents ignored)."""
    pattern = r"(?<!\w)" + re.escape(fold(term)) + r"(?!\w)"
    return re.search(pattern, fold(text)) is not None


def name_list(names: Sequence[str]) -> str:
    """Names joined as `A, B and C`."""
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _when(moment: Any) -> str:
    value = moment.to_native() if hasattr(moment, "to_native") else moment
    return value.astimezone(UTC).strftime("%H:%M UTC")


# --- Reading the snapshot ---------------------------------------------------------------

EVENTS_IN = """
MATCH (ev:Event) WHERE ev.started_at >= $start AND ev.started_at < $end
RETURN ev.key AS key, ev.title AS title, ev.page_count AS pages, ev.started_at AS started_at
ORDER BY ev.page_count DESC, ev.started_at DESC
"""
LATEST_EVENTS = """
MATCH (ev:Event)
RETURN ev.key AS key, ev.title AS title, ev.page_count AS pages, ev.started_at AS started_at
ORDER BY ev.started_at DESC LIMIT $limit
"""
EVENTS_WITH_ENTITIES = """
MATCH (n:Entity)-[r:INVOLVED_IN]->(ev:Event)
WHERE n.wikidata_id IS NOT NULL AND NOT ev.number IN $tuned
WITH ev, n, r ORDER BY r.confidence DESC, n.name
WITH ev, collect({qid: n.wikidata_id, name: n.name, type: n.type}) AS entities
WHERE size(entities) >= $minimum
OPTIONAL MATCH (p:Page)-[po:PART_OF]->(ev)
WITH ev, entities, p ORDER BY po.score DESC
WITH ev, entities, collect(p.title) AS pages
RETURN ev.key AS key, ev.number AS number, ev.title AS title, ev.page_count AS page_count,
       entities, pages[0] AS top_page
ORDER BY ev.page_count DESC, size(entities) DESC, ev.number
"""
ALL_INVOLVED = """
MATCH (n:Entity)-[r:INVOLVED_IN]->(ev:Event {key: $key})
RETURN n.name AS name ORDER BY r.confidence DESC, n.name
"""
SHARED_EVENT_PAIRS = """
MATCH (a:Entity)-[:INVOLVED_IN]->(ev:Event)<-[:INVOLVED_IN]-(b:Entity)
WHERE a.key < b.key AND a.wikidata_id IS NOT NULL AND b.wikidata_id IS NOT NULL
  AND a.type IN ['person', 'organization'] AND b.type IN ['person', 'organization']
  AND NOT ev.number IN $tuned AND NOT (a)-[:RELATED_TO]-(b)
RETURN a.name AS a, a.wikidata_id AS a_id, b.name AS b, b.wikidata_id AS b_id,
       ev.key AS event_key, ev.title AS event_title
ORDER BY ev.page_count DESC, ev.number, a.name, b.name
"""
RELATED_PAIRS = """
MATCH (a:Entity)-[r:RELATED_TO]->(b:Entity), (ev:Event {key: r.event_key})
WHERE a <> b AND a.wikidata_id IS NOT NULL AND b.wikidata_id IS NOT NULL
  AND a.type IN ['person', 'organization'] AND NOT ev.number IN $tuned
RETURN a.name AS a, a.wikidata_id AS a_id, b.name AS b, b.wikidata_id AS b_id,
       r.relation AS relation, ev.key AS event_key, ev.title AS event_title
ORDER BY ev.page_count DESC, ev.number, a.name, b.name
"""
TWO_HOP_PAIRS = """
MATCH (a:Entity)-[r1:RELATED_TO]-(x:Entity)-[r2:RELATED_TO]-(b:Entity)
WHERE a.key < b.key AND r1.event_key <> r2.event_key
  AND a.wikidata_id IS NOT NULL AND b.wikidata_id IS NOT NULL
  AND x.wikidata_id IS NOT NULL
  AND a.type IN ['person', 'organization'] AND b.type IN ['person', 'organization']
  AND NOT (a)-[:RELATED_TO]-(b)
  AND NOT EXISTS { MATCH (a)-[:INVOLVED_IN]->(:Event)<-[:INVOLVED_IN]-(b) }
WITH a, b, x, r1, r2, COUNT { (x)-[:RELATED_TO]-() } AS degree
RETURN a.name AS a, a.wikidata_id AS a_id, b.name AS b, b.wikidata_id AS b_id,
       x.name AS x, x.wikidata_id AS x_id,
       startNode(r1).name + ' ' + r1.relation + ' ' + endNode(r1).name AS first,
       startNode(r2).name + ' ' + r2.relation + ' ' + endNode(r2).name AS second,
       r1.event_key AS first_event, r2.event_key AS second_event
ORDER BY degree, a.name, b.name, x.name, first, second
"""
ENTITY_EVENT_COUNTS = """
MATCH (n:Entity {type: $type})-[:INVOLVED_IN]->(ev:Event)
WHERE ev.started_at >= $start AND ev.started_at < $end
WITH n, collect(DISTINCT ev.key) AS events
RETURN n.name AS name, n.wikidata_id AS qid, size(events) AS count, events
ORDER BY count DESC, name
"""
BIGGEST_EVENT = """
MATCH (ev:Event)
RETURN ev.key AS key, ev.title AS title, ev.page_count AS pages
ORDER BY ev.page_count DESC, ev.started_at DESC LIMIT 2
"""
ALL_TEXTS = """
CALL () {
  MATCH (n:Entity) RETURN n.name + ' | ' + reduce(s = '', a IN coalesce(n.aliases, []) |
    s + a + ' | ') AS text
  UNION ALL
  MATCH (p:Page) RETURN coalesce(p.title, '') + ' | ' + coalesce(p.description, '') + ' | '
    + coalesce(p.summary, '') AS text
  UNION ALL
  MATCH (ev:Event) RETURN coalesce(ev.title, '') + ' | ' + coalesce(ev.description, '')
    AS text
  UNION ALL
  MATCH (a)-[r:RELATED_TO]->(b) RETURN a.name + ' ' + r.relation + ' ' + b.name AS text
}
RETURN text
"""


class Snapshot:
    """Read-only access to the frozen graph for generating questions."""

    def __init__(self, driver: neo4j.Driver) -> None:
        self.driver = driver

    @classmethod
    def connect(cls, uri: str, settings: Settings) -> "Snapshot":
        auth = (settings.neo4j_user, settings.neo4j_password.get_secret_value())
        return cls(GraphDatabase.driver(uri, auth=auth))

    def close(self) -> None:
        self.driver.close()

    def rows(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        with self.driver.session(default_access_mode=neo4j.READ_ACCESS) as session:
            return [record.data() for record in session.run(cypher, params)]

    def clock(self) -> datetime:
        """The snapshot's "now", exactly as retrieval computes it (`QAGraph.clock`)."""
        row = self.rows("MATCH (ev:Event) RETURN max(ev.started_at) AS s, max(ev.ended_at) AS e")
        times = [t.to_native() for t in row[0].values() if t is not None]
        if not times:
            raise SystemExit("The snapshot has no events.")
        return max(times)

    def texts(self) -> list[str]:
        return [row["text"] for row in self.rows(ALL_TEXTS)]


# --- The generators, one per question type ------------------------------------------------


def draft(
    question: str,
    type_: QuestionType,
    expected_answer: str,
    derived_by: str,
    entities: Iterable[str] = (),
    events: Iterable[str] = (),
    answerable: bool = True,
) -> QAQuestion:
    """A question without its id yet."""
    return QAQuestion(
        id="",
        question=question,
        type=type_,
        expected_answer=expected_answer,
        expected_entities=list(dict.fromkeys(entities))[:MAX_EXPECTED],
        expected_events=list(dict.fromkeys(events))[:MAX_EXPECTED],
        answerable=answerable,
        source="synthetic",
        labeler="llm",
        derived_by=derived_by,
    )


def top_events(rows: Sequence[dict[str, Any]], k: int = 5) -> list[dict[str, Any]]:
    """The first `k` rows (biggest first, then latest, as retrieval orders them)."""
    return list(rows[:k])


def event_list(rows: Sequence[dict[str, Any]]) -> str:
    """Events as text: title, pages and start."""
    return "; ".join(
        f'"{r["title"]}" ({pages(r["pages"])}, {_when(r["started_at"])})' for r in rows
    )


def pages(count: int | None) -> str:
    """A page count as `1 page` or `N pages`."""
    return "1 page" if count == 1 else f"{count or 0} pages"


def period_question(
    snapshot: Snapshot, question: str, start: datetime, end: datetime, label: str
) -> QAQuestion:
    """A question about the events of a period, with its reference answer."""
    rows = snapshot.rows(EVENTS_IN, start=start, end=end)
    top = top_events(rows)
    if rows:
        answer = (
            f"{len(rows)} events started {label}. The biggest, by Wikipedia pages touched: "
            f"{event_list(top)}. A good answer names several of these."
        )
    else:
        answer = f"No event in the graph started {label}."
    derived = f"events with {start.isoformat()} <= started_at < {end.isoformat()}, biggest first"
    return draft(question, "time", answer, derived, events=[r["key"] for r in top])


def time_questions(snapshot: Snapshot, now: datetime) -> list[QAQuestion]:
    """The questions about periods relative to the graph's now."""
    today = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    day = today.strftime("%Y-%m-%d")
    later = now + timedelta(seconds=1)
    questions = [
        period_question(
            snapshot, "What happened today?", today, today + timedelta(days=1), f"on {day}"
        ),
        period_question(
            snapshot,
            "What happened in the last two hours?",
            now - timedelta(hours=2),
            later,
            f"between {_when(now - timedelta(hours=2))} and {_when(now)} on {day}",
        ),
        period_question(
            snapshot,
            "What happened between 20:00 and 21:00 UTC today?",
            today + timedelta(hours=20),
            today + timedelta(hours=21),
            f"between 20:00 and 21:00 UTC on {day}",
        ),
        period_question(
            snapshot,
            "What happened this afternoon (12:00 to 18:00 UTC)?",
            today + timedelta(hours=12),
            today + timedelta(hours=18),
            f"between 12:00 and 18:00 UTC on {day}",
        ),
    ]
    latest = snapshot.rows(LATEST_EVENTS, limit=5)
    answer = (
        f"The most recent events (newest first): {event_list(latest)}. A good answer names "
        "several of these recent stories, not just the day's biggest ones."
    )
    questions.append(
        draft(
            "What is the latest news?",
            "time",
            answer,
            "the 5 events with the latest started_at",
            events=[r["key"] for r in latest],
        )
    )
    return questions


def involved_answer(snapshot: Snapshot, event: dict[str, Any], who: str, names: list[str]) -> str:
    """The reference answer naming who is involved in an event."""
    everyone = [r["name"] for r in snapshot.rows(ALL_INVOLVED, key=event["key"])]
    answer = f'{who} in the event "{event["title"]}" ({event["key"]}): {name_list(names)}.'
    others = [n for n in everyone if n not in names]
    if who == "Entities involved" and others:
        answer += f" Also involved, without a Wikidata ID: {name_list(others)}."
    return answer


def event_questions(snapshot: Snapshot) -> list[QAQuestion]:
    """The questions about single events."""
    events = snapshot.rows(EVENTS_WITH_ENTITIES, tuned=list(TUNED_EVENT_NUMBERS), minimum=4)
    if len(events) < 5:
        raise SystemExit("The snapshot has too few events with linked entities.")
    derived = "INVOLVED_IN entities with a Wikidata ID, by confidence"
    questions = []
    phrasings: list[Callable[[dict[str, Any]], str]] = [
        lambda e: f'Who is involved in the event "{e["title"]}"?',
        lambda e: f"Who is involved in event {e['number']}?",
        lambda e: f'Who is involved in the news story about the Wikipedia page "{e["top_page"]}"?',
    ]
    for event, phrase in zip(events, phrasings, strict=False):
        entities = event["entities"][:MAX_EXPECTED]
        names = [e["name"] for e in entities]
        answer = involved_answer(snapshot, event, "Entities involved", names)
        questions.append(
            draft(
                phrase(event),
                "event",
                answer,
                derived,
                entities=[e["qid"] for e in entities],
                events=[event["key"]],
            )
        )
    rest = events[len(phrasings) :]
    for type_, noun, minimum, phrase in [
        ("person", "People involved", 3, lambda e: f"Which people are involved in event {e}?"),
        (
            "organization",
            "Organizations involved",
            2,
            lambda e: f'Which organizations are involved in the event "{e}"?',
        ),
    ]:
        for event in rest:
            chosen = [e for e in event["entities"] if e["type"] == type_]
            if len(chosen) >= minimum:
                rest = [e for e in rest if e is not event]
                subject = event["number"] if type_ == "person" else event["title"]
                answer = involved_answer(snapshot, event, noun, [e["name"] for e in chosen])
                questions.append(
                    draft(
                        phrase(subject),
                        "event",
                        answer,
                        f"{derived}, type {type_}",
                        entities=[e["qid"] for e in chosen],
                        events=[event["key"]],
                    )
                )
                break
    return questions


def first_new(
    rows: Iterable[dict[str, Any]], used_events: set[str], used_entities: set[str]
) -> dict[str, Any] | None:
    """The first pair whose event and entities no earlier question used."""
    for row in rows:
        events = {row.get("event_key"), row.get("first_event"), row.get("second_event")} - {None}
        if events & used_events or {row["a_id"], row["b_id"]} & used_entities:
            continue
        used_events.update(events)
        used_entities.update({row["a_id"], row["b_id"]})
        return row
    return None


SHORTEST_PATHS = """
MATCH (a:Entity {wikidata_id: $a}), (b:Entity {wikidata_id: $b})
MATCH p = allShortestPaths((a)-[:RELATED_TO|INVOLVED_IN*..4]-(b))
WITH [n IN nodes(p) | coalesce(n.name, n.title)] AS names,
     [r IN relationships(p) | CASE type(r) WHEN 'INVOLVED_IN' THEN 'involved in'
                                ELSE r.relation END] AS labels
RETURN DISTINCT names, labels ORDER BY names, labels LIMIT $limit
"""


def path_line(names: Sequence[str], labels: Sequence[str]) -> str:
    """ "A —involved in— Event —involved in— B" (direction left out)."""
    parts = [names[0]]
    for label, name in zip(labels, names[1:], strict=False):
        parts.append(f" —{label}— {name}")
    return "".join(parts)


def other_connections(snapshot: Snapshot, a_id: str, b_id: str, limit: int = 6) -> str:
    """Every shortest connection in the graph (as retrieval looks for them), so an
    answer that uses another true connection than the chosen one counts as correct."""
    rows = snapshot.rows(SHORTEST_PATHS, a=a_id, b=b_id, limit=limit)
    if len(rows) <= 1:
        return ""
    lines = "; ".join(path_line(r["names"], r["labels"]) for r in rows)
    return f" All shortest connections in the graph (any of them is a correct answer): {lines}."


def connection_questions(snapshot: Snapshot) -> list[QAQuestion]:
    """The questions about how two entities are connected."""
    tuned = list(TUNED_EVENT_NUMBERS)
    used_events: set[str] = set()
    used_entities: set[str] = set()
    questions = []
    shared = snapshot.rows(SHARED_EVENT_PAIRS, tuned=tuned)
    for phrase in ("How are {a} and {b} connected?", "What is the connection between {a} and {b}?"):
        row = first_new(shared, used_events, used_entities)
        if row is None:
            continue
        answer = (
            f'{row["a"]} and {row["b"]} are both involved in the event "{row["event_title"]}" '
            f"({row['event_key']})."
        ) + other_connections(snapshot, row["a_id"], row["b_id"])
        questions.append(
            draft(
                phrase.format(**row),
                "connection",
                answer,
                "two linked people/organizations INVOLVED_IN the same event, no direct relation",
                entities=[row["a_id"], row["b_id"]],
                events=[row["event_key"]],
            )
        )
    related = snapshot.rows(RELATED_PAIRS, tuned=tuned)
    for phrase in ("How is {a} related to {b}?", "What links {a} and {b}?"):
        row = first_new(related, used_events, used_entities)
        if row is None:
            continue
        answer = (
            f'{row["a"]} {row["relation"]} {row["b"]} (in the event "{row["event_title"]}", '
            f"{row['event_key']})."
        ) + other_connections(snapshot, row["a_id"], row["b_id"])
        questions.append(
            draft(
                phrase.format(**row),
                "connection",
                answer,
                "a RELATED_TO relationship between two linked entities",
                entities=[row["a_id"], row["b_id"]],
                events=[row["event_key"]],
            )
        )
    row = first_new(snapshot.rows(TWO_HOP_PAIRS), used_events, used_entities)
    if row is not None:
        answer = (
            f"They are connected through {row['x']}: {row['first']} "
            f"({row['first_event']}), and {row['second']} ({row['second_event']})."
        ) + other_connections(snapshot, row["a_id"], row["b_id"])
        questions.append(
            draft(
                f"How are {row['a']} and {row['b']} connected?",
                "connection",
                answer,
                "two RELATED_TO steps through a third entity, from two different events; "
                "the two share no event",
                entities=[row["a_id"], row["x_id"], row["b_id"]],
                events=[row["first_event"], row["second_event"]],
            )
        )
    return questions


def counted(rows: Sequence[dict[str, Any]]) -> str:
    """Names with their event counts, as text."""
    return "; ".join(f"{r['name']} ({r['count']} events)" for r in rows)


def aggregation_questions(snapshot: Snapshot, now: datetime) -> list[QAQuestion]:
    """The questions that count or rank across events."""
    today = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    window = {"start": today, "end": today + timedelta(days=1)}
    questions = []
    places = [
        r for r in snapshot.rows(ENTITY_EVENT_COUNTS, type="place", **window) if r["count"] >= 3
    ]
    questions.append(
        draft(
            "Which places appear in at least three events today?",
            "aggregation",
            f"{len(places)} places: {counted(places)}.",
            "entities of type place INVOLVED_IN at least 3 of today's events",
            entities=[r["qid"] for r in places if r["qid"]],
        )
    )
    today_events = snapshot.rows(EVENTS_IN, **window)
    questions.append(
        draft(
            "How many events started today?",
            "aggregation",
            f"{len(today_events)} events started on {today:%Y-%m-%d}.",
            "count of events with started_at today",
        )
    )
    biggest = snapshot.rows(BIGGEST_EVENT)
    top = biggest[0]
    runner_up = biggest[1] if len(biggest) > 1 else None
    margin = f" The next biggest touched {runner_up['pages']}." if runner_up else ""
    questions.append(
        draft(
            "Which event touched the most Wikipedia pages?",
            "aggregation",
            f'"{top["title"]}" ({top["key"]}), with {top["pages"]} pages.{margin}',
            "the event with the highest page_count",
            events=[top["key"]],
        )
    )
    people = snapshot.rows(ENTITY_EVENT_COUNTS, type="person", **window)
    leaders = [r for r in people if r["count"] == people[0]["count"]]
    questions.append(
        draft(
            "Which person is involved in the most events?",
            "aggregation",
            f"{counted(leaders)}. The next people have {people[len(leaders)]['count']} events."
            if len(people) > len(leaders)
            else f"{counted(leaders)}.",
            "people by the number of events they are INVOLVED_IN",
            entities=[r["qid"] for r in leaders if r["qid"]],
            events=[key for r in leaders for key in r["events"]],
        )
    )
    orgs = snapshot.rows(ENTITY_EVENT_COUNTS, type="organization", **window)
    org = next(r for r in orgs if r["qid"])
    titles = [
        r["title"] for r in snapshot.rows(EVENTS_IN, **window) if r["key"] in set(org["events"])
    ]
    questions.append(
        draft(
            f"How many events is {org['name']} involved in?",
            "aggregation",
            f"{org['name']} is involved in {org['count']} events: "
            + "; ".join(f'"{t}"' for t in titles)
            + ".",
            "the organization in the most events; its INVOLVED_IN events",
            entities=[org["qid"]],
            events=org["events"],
        )
    )
    return questions


class Unanswerable(BaseModel):
    """A question about something the snapshot must not mention (checked)."""

    question: str
    absent: list[str]  # no entity, page, event or relation text may contain these
    anchor: str | None = None  # if set, only texts that mention it are searched
    expected_answer: str


UNANSWERABLE = [
    Unanswerable(
        question="What did the Federal Reserve decide about interest rates today?",
        absent=["Federal Reserve", "interest rate", "interest rates"],
        expected_answer="The graph has no facts about the Federal Reserve or interest rates.",
    ),
    Unanswerable(
        question="Who won the Eurovision Song Contest 2026?",
        absent=["Eurovision Song Contest 2026", "Eurovision 2026"],
        expected_answer="The graph has no facts about the Eurovision Song Contest 2026.",
    ),
    Unanswerable(
        question="Who was the chief executive of Ueno Bank?",
        absent=["chief executive", "CEO", "chief executive officer", "managing director"],
        anchor="Ueno",
        expected_answer=(
            "The graph knows Ueno Bank (a Paraguayan bank that collapsed), but has no fact "
            "about its chief executive."
        ),
    ),
    Unanswerable(
        question="What did Apple announce at its product event today?",
        absent=["Apple Inc.", "Apple", "iPhone", "Tim Cook"],
        expected_answer="The graph has no facts about Apple or a product event.",
    ),
]


def found_terms(texts: Iterable[str], absent: Sequence[str], anchor: str | None) -> list[str]:
    """The `absent` terms that do occur (in texts mentioning `anchor`, if given)."""
    scope = [t for t in texts if anchor is None or mentions(t, anchor)]
    return [term for term in absent if any(mentions(text, term) for text in scope)]


def unanswerable_questions(snapshot: Snapshot, now: datetime) -> list[QAQuestion]:
    """Questions the graph can't answer (checked to be absent)."""
    texts = snapshot.texts()
    questions = []
    for item in UNANSWERABLE:
        present = found_terms(texts, item.absent, item.anchor)
        if present:
            raise SystemExit(f"{item.question!r} is answerable: the snapshot mentions {present}")
        scope = f" in texts mentioning {item.anchor!r}" if item.anchor else ""
        derived = f"checked: no entity, page, event or relation text{scope} mentions " + ", ".join(
            repr(term) for term in item.absent
        )
        questions.append(
            draft(item.question, "unanswerable", item.expected_answer, derived, answerable=False)
        )
    today = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = snapshot.rows(EVENTS_IN, start=today - timedelta(days=1), end=today)
    if yesterday:
        raise SystemExit("'What happened yesterday?' is answerable in this snapshot.")
    questions.insert(
        2,
        draft(
            "What happened yesterday?",
            "unanswerable",
            f"The graph has no events from {today - timedelta(days=1):%Y-%m-%d}; its events "
            f"all started on {today:%Y-%m-%d}.",
            "checked: no event started the day before the snapshot's newest event",
            answerable=False,
        ),
    )
    return questions


def generate(snapshot: Snapshot, snapshot_name: str) -> list[QAQuestion]:
    """All questions, numbered q01, q02, ... in type order."""
    now = snapshot.clock()
    groups = [
        time_questions(snapshot, now),
        event_questions(snapshot),
        connection_questions(snapshot),
        aggregation_questions(snapshot, now),
        unanswerable_questions(snapshot, now),
    ]
    questions = [q for group in groups for q in group]
    return [
        q.model_copy(update={"id": f"q{number:02d}", "snapshot": snapshot_name})
        for number, q in enumerate(questions, start=1)
    ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--path", type=Path, default=DEFAULT_QUESTIONS_PATH)
    parser.add_argument("--neo4j-uri", help="the snapshot (default: EVAL_QA_NEO4J_URI)")
    parser.add_argument("--snapshot", default="", help="snapshot name to record (e.g. 2026-11-05)")
    parser.add_argument("--force", action="store_true", help="replace the synthetic questions")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    setup_logging()
    logging.getLogger("neo4j").setLevel(logging.WARNING)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    existing = load_questions(args.path)
    if any(q.source == "synthetic" for q in existing) and not args.force:
        print(f"{args.path} already has synthetic questions; --force replaces them.")
        return 1
    snapshot = Snapshot.connect(args.neo4j_uri or settings.eval_qa_neo4j_uri, settings)
    try:
        name = args.snapshot or snapshot.clock().strftime("%Y-%m-%d")
        questions = merge_questions(existing, generate(snapshot, name))
    finally:
        snapshot.close()
    write_questions(args.path, questions)
    counts = {t: sum(q.type == t for q in questions) for t in QUESTION_TYPES}
    print(f"Wrote {len(questions)} questions to {args.path}: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

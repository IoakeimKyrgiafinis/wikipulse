"""Where briefings go: properties of the event's node in Neo4j.

The agent's only write to the graph. It never removes anything; it sets these
properties on one `Event` (the one the code chose, never one the model named):

- `briefing`: the text with [n] markers, then "Sources:" and one line per [n] (page
  title, revision, permanent link). The MCP tool `get_event` shows exactly this.
- `briefing_sources` (page titles) and `briefing_urls` (permanent revision links), in
  [n] order, for code that wants the citations without parsing text;
- `briefing_model`, `briefing_prompt_version`, `briefing_written_at` (wall clock:
  metadata only), `briefing_transcript` (the file in data/agent_runs/),
  `briefing_verified` (whether the grounding check ran and passed it);
- `briefing_for`: the event's `extracted_at` when the briefing was written. When stage 4
  extracts the event again (its pages changed), the two differ and the trigger writes a
  new briefing.

`candidates()` is the trigger's query: significant events (strong enough by spike score
or by number of pages) whose briefing is missing or out of date.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import neo4j
from neo4j import Driver

from wikipulse.agent.briefing import Briefing

logger = logging.getLogger(__name__)

CANDIDATES = """
MATCH (ev:Event)
WHERE $run_id IS NULL OR ev.run_id = $run_id
OPTIONAL MATCH (:Page)-[po:PART_OF]->(ev)
WITH ev, max(po.score) AS score
WHERE (score >= $min_score OR ev.page_count >= $min_pages)
  AND ($force OR NOT coalesce(properties(ev)['briefing_for'] = ev.extracted_at, false))
RETURN ev.key AS key, toString(ev.extracted_at) AS version, ev.correlation_id AS correlation_id
ORDER BY ev.page_count DESC, score DESC, ev.started_at
LIMIT $limit
"""
CORRELATION_ID = """
MATCH (ev:Event {key: $key})
RETURN ev.correlation_id AS correlation_id
"""
SAVE = """
MATCH (ev:Event {key: $key})
SET ev.briefing = $text,
    ev.briefing_sources = $titles,
    ev.briefing_urls = $urls,
    ev.briefing_model = $model,
    ev.briefing_prompt_version = $prompt_version,
    ev.briefing_written_at = $written_at,
    ev.briefing_transcript = $transcript,
    ev.briefing_verified = $verified,
    ev.briefing_for = ev.extracted_at
RETURN ev.key AS key
"""


@dataclass(frozen=True)
class Candidate:
    key: str
    version: str | None  # the event's extraction time: a new one means "brief again"
    correlation_id: str | None = None  # the edit that started the story (spec 09b)


@dataclass(frozen=True)
class BriefingMeta:
    model: str
    prompt_version: str
    written_at: datetime
    transcript: str | None = None


class BriefingStore:
    """Reads candidates and writes briefings in Neo4j."""

    def __init__(self, driver: Driver, database: str | None = None) -> None:
        self.driver = driver
        self.database = database

    def candidates(
        self,
        min_score: float,
        min_pages: int,
        limit: int = 50,
        run_id: str | None = None,
        force: bool = False,
    ) -> list[Candidate]:
        """Significant events without an up-to-date briefing, biggest first."""
        params: dict[str, Any] = {
            "min_score": min_score,
            "min_pages": min_pages,
            "limit": limit,
            "run_id": run_id,
            "force": force,
        }
        with self.driver.session(
            database=self.database, default_access_mode=neo4j.READ_ACCESS
        ) as session:
            rows = [record.data() for record in session.run(CANDIDATES, params)]
        return [Candidate(row["key"], row["version"], row["correlation_id"]) for row in rows]

    def correlation_id(self, key: str) -> str | None:
        """The correlation id of event `key` (None if unknown or not set)."""
        with self.driver.session(
            database=self.database, default_access_mode=neo4j.READ_ACCESS
        ) as session:
            record = session.run(CORRELATION_ID, key=key).single()
        return None if record is None else record["correlation_id"]

    def save(self, key: str, briefing: Briefing, meta: BriefingMeta) -> bool:
        """Set the briefing properties on event `key`; False if there is no such event."""
        params = {
            "key": key,
            "text": briefing.as_text(),
            "titles": [c.title for c in briefing.citations],
            "urls": [c.url for c in briefing.citations],
            "model": meta.model,
            "prompt_version": meta.prompt_version,
            "written_at": meta.written_at,
            "transcript": meta.transcript,
            "verified": briefing.verified,
        }
        with self.driver.session(database=self.database) as session:
            saved = session.execute_write(lambda tx: tx.run(SAVE, params).data())
        if not saved:
            logger.warning("No event %s in the graph; briefing not saved", key)
        return bool(saved)

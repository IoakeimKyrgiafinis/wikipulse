"""Information extraction: one LLM call per event turns its pages into entities and facts.

The model gets the prompt `prompts/extract_v1.md` as its system prompt and the event's
pages as numbered SOURCEs (`build_message`). It must answer with JSON that fits
`extraction_schema()`. The schema is not just documentation: Ollama constrains decoding
with it, so the model *can't* invent an entity type, cite a source that doesn't exist or
write a confidence like "pretty sure". What the schema can't express (a relation naming
an entity that isn't in the list, duplicates, empty names) is cleaned up afterwards by
`parse_extraction()`.

The LLM only runs here, once per event (and again only when the event's spikes change),
never on the raw edit stream: on 5 November 2026 the stream had millions of edits, the
detector about 665 spikes, the grouper about 570 events.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wikipulse.graph.inputs import EventInput, EventPage, trim
from wikipulse.graph.names import clean_name, is_proper_name, normalize_name
from wikipulse.llm import LLMClient, Usage

logger = logging.getLogger(__name__)

PROMPT_VERSION = "extract_v1"
PROMPTS_DIR = Path(__file__).parent / "prompts"
ENTITY_TYPES = ("person", "organization", "place", "event", "other")
# The model states its certainty in words; numbers from a small model would be noise.
CONFIDENCE = {"high": 0.9, "medium": 0.6, "low": 0.3}
MAX_ITEMS = 15
# Enough for 15 entities and 15 relations; a cut-off answer is retried by the client.
MAX_TOKENS = 2048


def load_prompt(version: str = PROMPT_VERSION) -> str:
    """The system prompt `prompts/<version>.md`."""
    return (PROMPTS_DIR / f"{version}.md").read_text(encoding="utf-8")


def extraction_schema(sources: int) -> dict[str, Any]:
    """The JSON schema of an answer for an event with `sources` pages."""
    source = {"type": "integer", "enum": list(range(1, sources + 1))}
    certainty = {"type": "string", "enum": list(CONFIDENCE)}
    name = {"type": "string", "minLength": 1, "maxLength": 120}

    def item(properties: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }

    entity = item(
        {"name": name, "type": {"type": "string", "enum": list(ENTITY_TYPES)}}
        | {"source": source, "certainty": certainty}
    )
    relation = item(
        {"subject": name, "relation": {"type": "string", "minLength": 2, "maxLength": 60}}
        | {"object": name, "source": source, "certainty": certainty}
    )
    return item(
        {
            "title": {"type": "string", "minLength": 3, "maxLength": 150},
            "description": {"type": "string", "minLength": 10, "maxLength": 500},
            "entities": {"type": "array", "items": entity, "maxItems": MAX_ITEMS},
            "relations": {"type": "array", "items": relation, "maxItems": MAX_ITEMS},
        }
    )


def source_block(number: int, page: EventPage, max_chars: int) -> str:
    """One SOURCE of the user message."""
    lines = [f"SOURCE {number}: {page.title}"]
    if page.description:
        lines.append(f"Short description: {page.description}")
    lines.append(f"Summary: {trim(page.summary or '(no summary)', max_chars)}")
    lines.append(f"Text added during the burst of edits:\n{page.changes or '(none available)'}")
    return "\n".join(lines)


def build_message(event: EventInput, pages: Sequence[EventPage], max_chars: int) -> str:
    """The user message: when the burst happened, then the numbered sources."""
    header = (
        f"These {len(pages)} article(s) were edited heavily from "
        f"{event.started_at:%Y-%m-%d %H:%M} UTC."
    )
    blocks = [source_block(n, page, max_chars) for n, page in enumerate(pages, start=1)]
    return "\n\n".join([header, *blocks])


@dataclass(frozen=True)
class ExtractedEntity:
    name: str
    type: str
    source_title: str
    source_revision: int | None
    confidence: float


@dataclass(frozen=True)
class ExtractedRelation:
    subject: str  # an entity's name, as in `Extraction.entities`
    relation: str
    object: str
    source_title: str
    source_revision: int | None
    confidence: float


@dataclass
class Extraction:
    """What the model found in one event, cleaned up."""

    title: str
    description: str
    entities: list[ExtractedEntity] = field(default_factory=list)
    relations: list[ExtractedRelation] = field(default_factory=list)
    model: str = ""
    prompt_version: str = PROMPT_VERSION
    usage: Usage = field(default_factory=Usage)


def clean_relation(text: str) -> str:
    return " ".join(text.strip().lower().split())


def parse_extraction(data: dict[str, Any], pages: Sequence[EventPage]) -> Extraction:
    """An Extraction from a schema-valid answer: sources resolved to pages, names cleaned,
    duplicates merged, entities a relation names but the list forgot added as "other".
    Generic nouns and dates are dropped (`is_proper_name`), with their relations."""
    sources = {n: page for n, page in enumerate(pages, start=1)}

    def provenance(item: dict[str, Any]) -> dict[str, Any]:
        page = sources.get(item["source"], pages[0])
        return {
            "source_title": page.title,
            "source_revision": page.revision,
            "confidence": CONFIDENCE[item["certainty"]],
        }

    entities: dict[str, ExtractedEntity] = {}  # normalized name -> entity

    def add_entity(name: str, type_: str, item: dict[str, Any]) -> str | None:
        key = normalize_name(name)
        if not key or not is_proper_name(name):
            return None  # e.g. "the police" or "October 15, 2026"
        found = entities.get(key)
        candidate = ExtractedEntity(clean_name(name), type_, **provenance(item))
        if found is None or candidate.confidence > found.confidence:
            entities[key] = candidate if found is None else _keep_type(candidate, found)
        return entities[key].name

    for item in data["entities"]:
        add_entity(item["name"], item["type"], item)
    relations: dict[tuple[str, str, str], ExtractedRelation] = {}
    for item in data["relations"]:
        subject = add_entity(item["subject"], "other", item)
        object_ = add_entity(item["object"], "other", item)
        relation = clean_relation(item["relation"])
        if not subject or not object_ or subject == object_ or not relation:
            continue
        key = (normalize_name(subject), relation, normalize_name(object_))
        if key not in relations:
            relations[key] = ExtractedRelation(subject, relation, object_, **provenance(item))
    return Extraction(
        title=" ".join(data["title"].split()),
        description=" ".join(data["description"].split()),
        entities=list(entities.values()),
        relations=list(relations.values()),
    )


def _keep_type(new: ExtractedEntity, old: ExtractedEntity) -> ExtractedEntity:
    """The more confident mention, but the type and name the entity list gave first."""
    return ExtractedEntity(
        old.name, old.type, new.source_title, new.source_revision, new.confidence
    )


class Extractor:
    """Asks an LLM (through `wikipulse.llm`) for the entities and facts of an event."""

    def __init__(
        self, client: LLMClient, max_chars: int = 1200, prompt_version: str = PROMPT_VERSION
    ) -> None:
        self.client = client
        self.max_chars = max_chars
        self.prompt_version = prompt_version
        self.prompt = load_prompt(prompt_version)

    def extract(self, event: EventInput, pages: Sequence[EventPage]) -> Extraction:
        """Extract from `pages` (the event's pages to show, at least one).

        Raises `wikipulse.llm.LLMError` if the model fails or gives no valid answer.
        """
        if not pages:
            raise ValueError(f"event {event.key} has no pages")
        response = self.client.chat(
            build_message(event, pages, self.max_chars),
            system=self.prompt,
            json_schema=extraction_schema(len(pages)),
            temperature=0.0,
            max_tokens=MAX_TOKENS,
        )
        extraction = parse_extraction(response.data, pages)
        extraction.model = self.client.setting
        extraction.prompt_version = self.prompt_version
        extraction.usage = response.usage
        logger.debug(
            "Event %s: %d entities, %d relations, %d tokens out",
            event.key,
            len(extraction.entities),
            len(extraction.relations),
            response.usage.output_tokens,
        )
        return extraction

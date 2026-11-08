"""Hand-made extractions for the extraction evaluation tests."""

from datetime import UTC, datetime

from wikipulse.eval.extraction_data import EntityView, EventView, ExtractionData, RelationView

RUN = "03b-test"
T0 = datetime(2026, 11, 5, 19, 0, tzinfo=UTC)


def ent(
    key: str,
    name: str,
    type_: str = "person",
    method: str = "link",
    aliases: tuple[str, ...] | None = None,
) -> EntityView:
    """An entity; linked (key = Wikidata ID) unless the key starts with `name:`."""
    linked = not key.startswith("name:")
    return EntityView(
        key=key,
        name=name,
        wikidata_id=key if linked else None,
        type=type_,
        link_method=method if linked else "none",
        aliases=aliases or (name,),
    )


def rel(subject: str, relation: str, object_: str, event: int = 1, revision: int = 100):
    return RelationView(
        event_key=f"{RUN}:{event}",
        subject=subject,
        relation=relation,
        object=object_,
        source_title="Source page",
        source_revision=revision,
        confidence=0.9,
    )


def data(entities: list[EntityView], relations: list[RelationView]) -> ExtractionData:
    """One event holding all entities and relations."""
    return ExtractionData(
        source="graph",
        run_id=RUN,
        events=[EventView(f"{RUN}:1", 1, "Title", "Description.", T0)],
        entities={e.key: e for e in entities},
        mentions={f"{RUN}:1": [e.key for e in entities]},
        relations=relations,
        models=["ollama:qwen3:4b"],
        prompt_versions=["extract_v1"],
    )


# The Paraguay story of 5 November 2026 (event 341), as the graph stored it.
PENA = ent("Q22695687", "Santiago Peña", "person", "page")
ALLIANA = ent("Q51885479", "Pedro Alliana", "person", "link")
PARAGUAY = ent("Q733", "Paraguay", "place", "link")
PRESIDENT = ent("Q34071", "President of Paraguay", "other", "link")
UENO = ent("name:ueno bank", "Ueno Bank", "organization")
GEORGIA = ent("Q1428", "Georgia", "place", "search")  # the US state

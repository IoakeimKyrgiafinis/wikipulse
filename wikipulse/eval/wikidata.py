"""Wikidata as ground truth for the knowledge graph, through its SPARQL query service.

Two questions are asked, each in batches (one request answers many names or pairs):

- **Which items carry this name as their label?** (`Wikidata.items_by_label`) An item
  counts if its English (`en`) or language-neutral (`mul`) label is the name, or the
  name with its first letter in the other case: Wikipedia titles start with a capital
  ("Frozen yogurt"), Wikidata labels of common nouns don't ("frozen yogurt").
  Wikimedia's own items (disambiguation pages, categories, templates, lists) and name
  items (family names, given names) are left out: they share a name with everything but
  are never the thing itself.
- **Which statements connect two items?** (`Wikidata.statements_between`) Any property,
  in either direction: `?a wdt:P ?b`. These are the "truthy" statements, the best rank
  of each property (the preferred ones, else the normal ones). Asking for every rank
  (`?a p:P ?s . ?s ps:P ?b` with the property left open) timed out after 60 s even for
  ten pairs, because big items like the United States have thousands of statements.

Why SPARQL and not the Action API (`wbsearchentities`, `wbgetentities`)? Search is a
ranked prefix search, so "all items with exactly this label" is not guaranteed, and the
entity JSON of a big item (the United States) is megabytes. A SPARQL query answers
exactly the question, for 25 names or 40 pairs at once, in a few kilobytes. Every request
goes through the polite, cached `WikimediaClient` (User-Agent, one at a time, retries).
"""

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from wikipulse.enrich.client import WikimediaClient

logger = logging.getLogger(__name__)

ENTITY_PREFIX = "http://www.wikidata.org/entity/"
# Names per label query and item pairs per statement query (keeps GET URLs short).
NAMES_PER_QUERY = 25
PAIRS_PER_QUERY = 40
# Items that share a label with everything but are never the entity a story means.
EXCLUDED_CLASSES = {
    "Q4167410": "Wikimedia disambiguation page",
    "Q4167836": "Wikimedia category",
    "Q11266439": "Wikimedia template",
    "Q13406463": "Wikimedia list article",
    "Q101352": "family name",
    "Q202444": "given name",
    "Q12308941": "male given name",
    "Q11879590": "female given name",
}
LABEL_LANGUAGES = ("en", "mul")


def qid(uri: str) -> str:
    """`http://www.wikidata.org/entity/Q30` -> `Q30`."""
    return uri.removeprefix(ENTITY_PREFIX)


def sparql_string(text: str) -> str:
    """A SPARQL string literal (JSON's escapes are valid SPARQL escapes)."""
    return json.dumps(text, ensure_ascii=False)


def label_variants(name: str) -> list[str]:
    """The name, and the name with its first letter in the other case."""
    name = " ".join(name.split())
    if not name:
        return []
    first = name[0]
    other = first.lower() if first.isupper() else first.upper()
    return [name] if other == first else [name, other + name[1:]]


def labels_query(names: Sequence[str]) -> str:
    """Items whose `en` or `mul` label is one of `names` (or a case variant)."""
    values = " ".join(
        f"{sparql_string(variant)}@{language}"
        for name in names
        for variant in label_variants(name)
        for language in LABEL_LANGUAGES
    )
    excluded = " ".join(f"wd:{item}" for item in EXCLUDED_CLASSES)
    return (
        "SELECT ?label ?item WHERE {\n"
        f"  VALUES ?label {{ {values} }}\n"
        "  ?item rdfs:label ?label .\n"
        f"  FILTER NOT EXISTS {{ ?item wdt:P31 ?class . VALUES ?class {{ {excluded} }} }}\n"
        "}"
    )


def bindings(body: Any) -> list[dict[str, Any]]:
    """The result rows of a SPARQL JSON answer."""
    if not isinstance(body, Mapping):
        return []
    return list(body.get("results", {}).get("bindings", []))


def parse_labels(names: Sequence[str], body: Any) -> dict[str, tuple[str, ...]]:
    """name -> the IDs of the items carrying it as a label (in answer order, no repeats).

    Every name gets an entry; a name no item carries maps to `()`.
    """
    variant_of: dict[str, list[str]] = {}
    for name in names:
        for variant in label_variants(name):
            variant_of.setdefault(variant, []).append(name)
    found: dict[str, list[str]] = {name: [] for name in names}
    for row in bindings(body):
        label = row.get("label", {}).get("value", "")
        item = qid(row.get("item", {}).get("value", ""))
        for name in variant_of.get(label, []):
            if item and item not in found[name]:
                found[name].append(item)
    return {name: tuple(items) for name, items in found.items()}


def statements_query(pairs: Sequence[tuple[str, str]]) -> str:
    """Truthy statements of item `a` whose value is item `b`, for each (a, b) given."""
    values = " ".join(f"(wd:{a} wd:{b})" for a, b in pairs)
    return (
        "SELECT DISTINCT ?a ?b ?property ?propertyLabel WHERE {\n"
        f"  VALUES (?a ?b) {{ {values} }}\n"
        "  ?a ?direct ?b .\n"
        "  ?property wikibase:directClaim ?direct .\n"
        '  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }\n'
        "}"
    )


@dataclass(frozen=True)
class Statement:
    """`subject property object` in Wikidata, e.g. Q22686 P102 Q29468."""

    subject: str
    property: str
    label: str  # the property's English label, e.g. "member of political party"
    object: str


def parse_statements(body: Any) -> list[Statement]:
    """The statements in a SPARQL answer."""
    statements = []
    for row in bindings(body):
        prop = qid(row.get("property", {}).get("value", ""))
        label = row.get("propertyLabel", {}).get("value", prop)
        a, b = qid(row["a"]["value"]), qid(row["b"]["value"])
        statements.append(Statement(a, prop, label, b))
    return statements


def batches(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    """`items` in slices of at most `size`."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


class Wikidata:
    """Asks the Wikidata Query Service, in batches, through a `WikimediaClient`."""

    def __init__(self, client: WikimediaClient, sparql_url: str) -> None:
        self.client = client
        self.sparql_url = sparql_url

    async def query(self, sparql: str) -> Any:
        response = await self.client.get_json(self.sparql_url, {"query": sparql, "format": "json"})
        return response.body

    async def items_by_label(self, names: Iterable[str]) -> dict[str, tuple[str, ...]]:
        """name -> IDs of the items whose label it is (see the module docstring)."""
        unique = sorted({" ".join(name.split()) for name in names if name.strip()})
        found: dict[str, tuple[str, ...]] = {}
        for batch in batches(unique, NAMES_PER_QUERY):
            found |= parse_labels(batch, await self.query(labels_query(batch)))
        logger.info("Looked up %d names in Wikidata", len(unique))
        return found

    async def statements_between(
        self, pairs: Iterable[tuple[str, str]]
    ) -> dict[frozenset[str], list[Statement]]:
        """{a, b} -> the statements connecting a and b, in either direction."""
        directed = sorted({(a, b) for x, y in pairs if x != y for a, b in ((x, y), (y, x))})
        found: dict[frozenset[str], list[Statement]] = {}
        for batch in batches(directed, PAIRS_PER_QUERY):
            for statement in parse_statements(await self.query(statements_query(batch))):
                key = frozenset((statement.subject, statement.object))
                found.setdefault(key, []).append(statement)
        logger.info("Looked up statements for %d item pairs in Wikidata", len(directed) // 2)
        return found


def describe_query(items: Sequence[str]) -> str:
    """English label and description of each item (for reading disagreements)."""
    values = " ".join(f"wd:{item}" for item in items)
    return (
        "SELECT ?item ?itemLabel ?itemDescription WHERE {\n"
        f"  VALUES ?item {{ {values} }}\n"
        '  SERVICE wikibase:label { bd:serviceParam wikibase:language "en,mul". }\n'
        "}"
    )


def parse_descriptions(body: Any) -> dict[str, str]:
    """item -> "label: description"."""
    found = {}
    for row in bindings(body):
        item = qid(row.get("item", {}).get("value", ""))
        label = row.get("itemLabel", {}).get("value", item)
        description = row.get("itemDescription", {}).get("value", "")
        found[item] = f"{label}: {description}" if description else label
    return found


async def describe(wikidata: Wikidata, items: Iterable[str]) -> dict[str, str]:
    """item -> "label: description", 50 items per request."""
    unique = sorted(set(items))
    found: dict[str, str] = {}
    for batch in batches(unique, 50):
        found |= parse_descriptions(await wikidata.query(describe_query(batch)))
    return found

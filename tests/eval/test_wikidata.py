"""Wikidata ground truth: SPARQL queries, and saved answers of the query service.

`tests/fixtures/eval/wikidata.json` holds real answers of query.wikidata.org (saved on
2026-11-08) to one label query and one statement query.
"""

import json
from pathlib import Path

import httpx

from tests.enrich.helpers import request_id
from wikipulse.enrich.cache import MemoryCache
from wikipulse.enrich.client import WikimediaClient
from wikipulse.eval.wikidata import (
    NAMES_PER_QUERY,
    Wikidata,
    describe_query,
    label_variants,
    labels_query,
    parse_descriptions,
    parse_labels,
    statements_query,
)

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "eval" / "wikidata.json"
SPARQL_URL = "https://query.wikidata.org/sparql"


class SavedSparql(httpx.MockTransport):
    """Answers from the fixture; any other query gets an empty result (and is recorded)."""

    def __init__(self) -> None:
        exchanges = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.saved = {request_id(e["url"], e["params"]): e["body"] for e in exchanges}
        self.queries: list[str] = []
        super().__init__(self.answer)

    def answer(self, request: httpx.Request) -> httpx.Response:
        self.queries.append(request.url.params["query"])
        key = request_id(str(request.url.copy_with(query=None)), dict(request.url.params))
        empty = {"head": {"vars": []}, "results": {"bindings": []}}
        return httpx.Response(200, json=self.saved.get(key, empty))


def wikidata(transport: SavedSparql) -> Wikidata:
    client = WikimediaClient(
        "WikiPulse tests", MemoryCache(), concurrency=1, min_interval_s=0, transport=transport
    )
    return Wikidata(client, SPARQL_URL)


NAMES = [
    "Bend",
    "Frozen yogurt",
    "Lisa Loeb",
    "Netanyahu",
    "Paraguay",
    "Pedro Alliana",
    "Santiago Peña",
    "Ueno Bank",
]


def test_a_name_is_also_looked_up_with_its_first_letter_in_the_other_case():
    assert label_variants("Frozen yogurt") == ["Frozen yogurt", "frozen yogurt"]
    assert label_variants("iPhone") == ["iPhone", "IPhone"]
    assert label_variants("2026 SAFF Championship") == ["2026 SAFF Championship"]
    query = labels_query(['Say "hi"'])
    assert '"Say \\"hi\\""@en' in query and '"Say \\"hi\\""@mul' in query
    assert "wd:Q4167410" in query  # no disambiguation pages


async def test_items_carrying_a_label_from_a_saved_answer():
    transport = SavedSparql()
    found = await wikidata(transport).items_by_label(NAMES)
    # Pedro Alliana has an `en` and a `mul` label: one item, listed once.
    assert found["Pedro Alliana"] == ("Q51885479",)
    # Two items carry "Santiago Peña": a single right answer doesn't exist.
    assert "Q22695687" in found["Santiago Peña"] and len(found["Santiago Peña"]) == 2
    assert "Q733" in found["Paraguay"] and len(found["Bend"]) > 10
    assert found["Ueno Bank"] == ()  # too new for Wikidata
    # "Netanyahu" is only a family name, and name items are left out.
    assert found["Netanyahu"] == ()
    assert found["Frozen yogurt"] == ("Q13319",)  # Wikidata's label is "frozen yogurt"
    assert len(transport.queries) == 1


def test_parse_labels_maps_case_variants_back_to_the_name_asked():
    body = {
        "results": {
            "bindings": [
                {
                    "label": {"xml:lang": "en", "value": "frozen yogurt"},
                    "item": {"value": "http://www.wikidata.org/entity/Q11735"},
                }
            ]
        }
    }
    assert parse_labels(["Frozen yogurt", "Gelato"], body) == {
        "Frozen yogurt": ("Q11735",),
        "Gelato": (),
    }


async def test_statements_in_either_direction_from_a_saved_answer():
    transport = SavedSparql()
    pairs = [("Q22695687", "Q733"), ("Q22686", "Q29468"), ("Q51885479", "Q34071")]
    found = await wikidata(transport).statements_between(pairs)
    trump = {s.property for s in found[frozenset(("Q22686", "Q29468"))]}
    assert "P102" in trump  # member of political party
    pena = found[frozenset(("Q22695687", "Q733"))]
    assert [s.label for s in pena] == ["country of citizenship"]
    # Both directions: Alliana holds the office, the office has him as its holder.
    alliana = {(s.subject, s.property) for s in found[frozenset(("Q51885479", "Q34071"))]}
    assert alliana == {("Q51885479", "P39"), ("Q34071", "P1308")}
    assert len(transport.queries) == 1
    assert statements_query([("Q1", "Q2")]).count("wd:Q1 wd:Q2") == 1


async def test_names_and_pairs_are_asked_in_batches():
    transport = SavedSparql()
    names = [f"Name {n}" for n in range(NAMES_PER_QUERY + 1)]
    found = await wikidata(transport).items_by_label(names + ["Name 0", "  "])
    assert len(found) == NAMES_PER_QUERY + 1
    assert len(transport.queries) == 2
    pairs = [(f"Q{n}", f"Q{n + 1000}") for n in range(25)]  # 50 directed pairs
    await wikidata(transport).statements_between(pairs + [("Q5", "Q5")])
    assert len(transport.queries) == 4


def test_descriptions_say_what_an_item_is():
    body = {
        "results": {
            "bindings": [
                {
                    "item": {"value": "http://www.wikidata.org/entity/Q38095"},
                    "itemLabel": {"value": "Galapagos Islands"},
                    "itemDescription": {"value": "Archipelago in the Pacific Ocean"},
                },
                {
                    "item": {"value": "http://www.wikidata.org/entity/Q1"},
                    "itemLabel": {"value": "Q1"},
                },
            ]
        }
    }
    assert parse_descriptions(body) == {
        "Q38095": "Galapagos Islands: Archipelago in the Pacific Ocean",
        "Q1": "Q1",
    }
    assert "wd:Q38095 wd:Q61" in describe_query(["Q38095", "Q61"])

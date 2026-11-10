"""The summary at the cutoff: wikitext to a lead, cached per revision; the embeddings."""

import json

import httpx
import numpy as np

from tests.ml.synthetic import fake_embedder
from wikipulse.eval.wiki_client import WikiClient
from wikipulse.ml.summaries import (
    Lead,
    LeadCache,
    embed_texts,
    fetch_leads,
    lead_of,
    short_description,
    summary_text,
    summary_texts,
)

WIKITEXT = """{{Short description|American singer (1950–2026)}}
{{Use mdy dates|date=November 2026}}
{{Infobox person
| name = Jane Doe
| image = {{Image|x.jpg}}
}}
[[File:Jane Doe 2019.jpg|thumb|Doe in [[Paris]], 2019]]
[[File:Jane Doe 1970.jpg|thumb|upright|Doe in 1970]]
'''Jane Doe''' (born 1950 – November 8, 2026)<ref name="obit">{{cite news|title=Obit}}</ref> \
was an [[United States|American]] singer.<ref>Plain ref</ref> She won <!-- hidden --> \
three [[Grammy Award]]s.{{citation needed|date=May 2020}}

{| class="wikitable"
| a || b
|}
Her best-known song is ''[[Song Title (song)|Song Title]]''. See [https://example.org the site].
__NOTOC__

== Early life ==
Doe was born in [[Ohio]].
[[Category:2026 deaths]]
"""


def test_short_description_and_lead_are_plain_text():
    assert short_description(WIKITEXT) == "American singer (1950–2026)"
    assert short_description("{{Short description|none}}") is None
    lead = lead_of(WIKITEXT)
    assert lead == (
        "Jane Doe (born 1950 – November 8, 2026) was an American singer. She won three "
        "Grammy Awards. Her best-known song is Song Title. See the site."
    )
    assert lead_of(WIKITEXT, max_chars=8) == "Jane Doe"


def test_summary_text_uses_title_description_and_lead():
    lead = Lead(1, False, "American singer", "Jane Doe was a singer.")
    assert summary_text("Jane Doe", lead) == "Jane Doe. American singer. Jane Doe was a singer."
    assert summary_text("Jane Doe", Lead(2, hidden=True)) == "Jane Doe"
    assert summary_texts(["A", "B"], [1, None], {1: lead}) == [
        "A. American singer. Jane Doe was a singer.",
        "B",
    ]


def answer(revids: list[int]) -> dict:
    pages = [
        {
            "pageid": 10,
            "revisions": [
                {"revid": r, "slots": {"main": {"content": f"'''Page {r}''' is a page."}}}
                for r in revids
                if r != 3
            ]
            + ([{"revid": 3, "slots": {"main": {"texthidden": True}}}] if 3 in revids else []),
        }
    ]
    return {"query": {"pages": pages}}


def test_fetch_leads_asks_once_per_revision_and_caches(tmp_path):
    asked: list[list[int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        revids = [int(r) for r in request.url.params["revids"].split("|")]
        asked.append(revids)
        return httpx.Response(200, json=answer(revids))

    cache = LeadCache(tmp_path / "leads")
    transport = httpx.MockTransport(handler)
    with WikiClient(
        "test-agent", tmp_path, 0.0, transport=transport, sleep=lambda s: None
    ) as client:
        leads = fetch_leads(client, "https://wiki.test/api.php", [1, 2, 3], cache)
        assert leads[1].lead == "Page 1 is a page."
        assert leads[3].hidden
        again = fetch_leads(client, "https://wiki.test/api.php", [1, 2, 3, 4], cache)
    assert asked == [[1, 2, 3], [4]]  # the second call only asks for the new revision
    assert again[2] == leads[2]
    saved = json.loads((tmp_path / "leads" / "1.json").read_text(encoding="utf-8"))
    assert saved["lead"] == "Page 1 is a page."


def test_embed_texts_returns_one_unit_row_per_text():
    matrix = embed_texts(["an event", "a thing", "an event"], fake_embedder())
    assert matrix.shape == (3, 8)
    assert np.allclose(np.linalg.norm(matrix, axis=1), 1.0)
    assert np.array_equal(matrix[0], matrix[2])


def test_leads_of_an_older_parser_are_parsed_again(tmp_path):
    cache = LeadCache(tmp_path)
    cache.put(Lead(5, False, None, "old text", parser=1))
    assert cache.get(5) is None
    cache.put(Lead(5, False, None, "new text"))
    assert cache.get(5).lead == "new text"

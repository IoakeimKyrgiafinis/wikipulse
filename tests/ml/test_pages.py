"""The page at its cutoff revision: wikitext parsing and the MediaWiki requests."""

from datetime import UTC, datetime

import httpx

from wikipulse.eval.wiki_client import WikiClient
from wikipulse.ml.pages import (
    REVISIONS_PER_REQUEST,
    Revision,
    RevisionCache,
    fetch_creation_times,
    fetch_revisions,
    parse_creation,
    parse_revisions,
    parse_wikitext,
    snapshot_of,
)

API = "https://en.wikipedia.org/w/api.php"

WIKITEXT = """{{Short description|American politician}}
{{Current|date=November 2026}}
{{Infobox officeholder
| name = Jane Example
}}
'''Jane Example''' (born 1960) is a politician.<ref>{{cite web|url=x}}</ref>
She was elected in 2026.<ref name="a">Source</ref><ref name="a" />
{{#if:x|y}}
[[Category:Living people]]
[[Category:1960 births|Example, Jane]]
[[category:2026_United States Senate elections]]
[[Category:Living people]]
"""


def test_parse_wikitext_finds_categories_templates_and_refs():
    categories, templates, refs = parse_wikitext(WIKITEXT)
    assert categories == ("Living people", "1960 births", "2026 United States Senate elections")
    assert templates[:3] == ("short description", "current", "infobox officeholder")
    assert "cite web" in templates
    assert not any(t.startswith("#") for t in templates)
    assert refs == 3


def revision_body(revids: list[int], hidden: int | None = None) -> dict:
    return {
        "query": {
            "pages": [
                {
                    "pageid": 7000 + revid,
                    "title": f"Page {revid}",
                    "revisions": [
                        {
                            "revid": revid,
                            "timestamp": "2026-11-06T14:05:00Z",
                            "slots": {
                                "main": {"texthidden": True}
                                if revid == hidden
                                else {"content": f"[[Category:Cat {revid}]]"}
                            },
                        }
                    ],
                }
                for revid in revids
            ]
        }
    }


def test_parse_revisions_and_hidden_text():
    revisions = parse_revisions(revision_body([1, 2], hidden=2))
    when = datetime(2026, 11, 6, 14, 5, tzinfo=UTC)
    assert revisions[0] == Revision(1, 7001, when, False, ("Cat 1",), (), 0)
    assert revisions[1].hidden
    assert snapshot_of(revisions[1], None) is None  # unknown, not "no categories"
    snapshot = snapshot_of(revisions[0], datetime(2020, 1, 1, tzinfo=UTC))
    assert snapshot.categories == ("Cat 1",) and snapshot.revision_id == 1


def test_revision_cache_round_trip(tmp_path):
    cache = RevisionCache(tmp_path / "revisions")
    revision = Revision(
        5, 70, datetime(2026, 11, 6, 14, 5, tzinfo=UTC), False, ("Living people",), ("current",), 3
    )
    assert cache.get(5) is None
    cache.put(revision)
    assert cache.get(5) == revision
    (tmp_path / "revisions" / "6.json").write_text("not json", encoding="utf-8")
    assert cache.get(6) is None  # unreadable: fetched again


def test_parse_creation():
    body = {
        "query": {"pages": [{"pageid": 5, "revisions": [{"timestamp": "2019-03-01T10:00:00Z"}]}]}
    }
    assert parse_creation(body) == datetime(2019, 3, 1, 10, tzinfo=UTC)
    assert parse_creation({"query": {"pages": [{"pageid": 5, "missing": True}]}}) is None


def fake_api(tmp_path, handler) -> WikiClient:
    transport = httpx.MockTransport(handler)
    return WikiClient("test-agent", tmp_path, min_interval_s=0, transport=transport)


def test_fetch_revisions_batches_follows_continue_and_caches_per_revision(tmp_path):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        requests.append(params)
        revids = [int(r) for r in params["revids"].split("|")]
        if "rvcontinue" not in params and len(revids) > 1:  # the API stops halfway
            body = revision_body(revids[:1])
            body["continue"] = {"rvcontinue": str(revids[1]), "continue": "||"}
            return httpx.Response(200, json=body)
        return httpx.Response(
            200, json=revision_body(revids[1:] if "rvcontinue" in params else revids)
        )

    wanted = list(range(1, REVISIONS_PER_REQUEST + 3))  # two batches
    cache = RevisionCache(tmp_path / "revisions")
    with fake_api(tmp_path / "http", handler) as client:
        found = fetch_revisions(client, API, wanted, cache)
        assert sorted(found) == wanted
        assert len(requests) == 4  # two batches, each continued once
        assert all(r["rvprop"] == "ids|timestamp|content" for r in requests)
        assert fetch_revisions(client, API, wanted, cache) == found
        assert len(requests) == 4  # second time: all from the cache
        more = fetch_revisions(client, API, [0, *wanted], cache)  # one new revision
        assert len(requests) == 5 and requests[-1]["revids"] == "0"
        assert sorted(more) == [0, *wanted]
    assert not (tmp_path / "http").exists()  # the bulky answers themselves are not kept


def test_fetch_creation_times_one_request_per_page(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        assert params["rvdir"] == "newer" and params["rvlimit"] == "1"
        pageid = int(params["pageids"])
        return httpx.Response(
            200,
            json={"query": {"pages": [{"pageid": pageid, "revisions": [
                {"revid": 1, "timestamp": f"20{pageid:02d}-01-01T00:00:00Z"}]}]}},
        )  # fmt: skip

    with fake_api(tmp_path, handler) as client:
        created = fetch_creation_times(client, API, [15, 3, 15])
    assert created == {3: datetime(2003, 1, 1, tzinfo=UTC), 15: datetime(2015, 1, 1, tzinfo=UTC)}

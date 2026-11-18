"""The static demo in `site/` and its exported data (scripts/export_demo.py).

Everything here reads committed files; no Postgres, Neo4j or network.
"""

import importlib.util
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
DAY_FILE = SITE / "data" / "day.json"
SCRIPT = ROOT / "scripts" / "export_demo.py"
spec = importlib.util.spec_from_file_location("export_demo", SCRIPT)
assert spec is not None and spec.loader is not None
export_demo = importlib.util.module_from_spec(spec)
sys.modules["export_demo"] = export_demo
spec.loader.exec_module(export_demo)

VENDOR_SCRIPT = ROOT / "scripts" / "vendor_relate.py"
vendor_spec = importlib.util.spec_from_file_location("vendor_relate", VENDOR_SCRIPT)
assert vendor_spec is not None and vendor_spec.loader is not None
vendor_relate = importlib.util.module_from_spec(vendor_spec)
sys.modules["vendor_relate"] = vendor_relate
vendor_spec.loader.exec_module(vendor_relate)

# Committed files under site/ (spec 10's cap). The vendored Relate build in site/vendor/
# (git-ignored, written by scripts/vendor_relate.py) has its own limit,
# vendor_relate.MAX_VENDOR_BYTES, and is only downloaded when the visitor opens the
# graph section.
MAX_SITE_BYTES = 3 * 1024 * 1024
VENDOR = SITE / "vendor"
# The page's own scripts (the vendored ones are checked by their pinned hashes).
SCRIPTS = sorted(SITE.glob("*.js"))
# Keys that would hold something about a person who edited, or the edit itself.
PERSONAL_KEYS = {
    "user",
    "username",
    "user_id",
    "user_text",
    "editor",
    "editors",
    "ip",
    "ip_address",
    "comment",
    "parsedcomment",
    "edit_summary",
    "summary",
    "correlation_id",
    "event_id",
    "meta",
}
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
IPV6 = re.compile(
    r"\b(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}\b"
    r"|\b[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{0,4}){2,6}::"
)
LOADING_TAG = re.compile(
    r"<(script|link|img|iframe|source|video|audio|embed|object)\b[^>]*?"
    r"(?:src|href|data)=\"([^\"]+)\""
)
LOADING_CALL = re.compile(r"(?:fetch|WebSocket|EventSource|importScripts|import)\(([^)]*)\)")
# Pages a visitor may *navigate* to. Nothing is ever *loaded* from another origin.
LINK_PREFIXES = (
    "https://en.wikipedia.org/",
    "https://www.wikidata.org/",
    "https://github.com/IoakeimKyrgiafinis/wikipulse",
    "https://creativecommons.org/licenses/by-sa/4.0/",
)
SVG_NAMESPACE = "http://www.w3.org/2000/svg"  # an XML name, not a request


@pytest.fixture(scope="module")
def day() -> dict:
    return json.loads(DAY_FILE.read_text(encoding="utf-8"))


def walk(value, path=""):
    """Every (path, key, value) in a JSON document."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield path, key, item
            yield from walk(item, f"{path}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from walk(item, f"{path}[{i}]")


def strings(value):
    return [v for _, _, v in walk(value) if isinstance(v, str)]


# --- The exported data's schema -------------------------------------------------------------
def test_the_export_matches_its_schema(day) -> None:
    export = export_demo.DemoDay.model_validate(day)  # extra="forbid" on every model
    assert export.schema_version == export_demo.SCHEMA_VERSION
    assert export.end > export.start
    assert export.events, "an export without events"


def test_the_counts_agree_with_the_events(day) -> None:
    events = day["events"]
    counts = day["counts"]
    assert counts["events"] == len(events)
    assert counts["spikes"] == sum(len(p["spikes"]) for e in events for p in e["pages"])
    assert counts["events_in_graph"] == sum(1 for e in events if e["headline"])
    assert counts["entities"] == sum(len(e["entities"]) for e in events)
    assert counts["relations"] == sum(len(e["relations"]) for e in events)
    assert counts["briefings"] == sum(1 for e in events if e["briefing"])


def test_every_event_is_replayable(day) -> None:
    """The page needs: pages with spikes, an event starting with its first spike, times
    in the day, links to Wikipedia."""
    start = datetime.fromisoformat(day["start"])
    end = datetime.fromisoformat(day["end"])
    numbers = [e["number"] for e in day["events"]]
    assert len(numbers) == len(set(numbers))
    for event in day["events"]:
        assert event["pages"], event["number"]
        spike_starts = [s["start"] for p in event["pages"] for s in p["spikes"]]
        assert min(spike_starts) == event["started_at"], event["number"]
        assert start <= datetime.fromisoformat(event["started_at"]) < end
        titles = [p["title"] for p in event["pages"]]
        assert len(titles) == len(set(titles)), event["number"]
        for page in event["pages"]:
            assert page["url"].startswith("https://en.wikipedia.org/wiki/")
            assert page["spikes"]
        for entity in event["entities"]:
            assert entity["wikidata_id"] is None or re.fullmatch(r"Q\d+", entity["wikidata_id"])
        if event["entities"] or event["relations"]:
            assert event["headline"], f"facts without a headline in {event['number']}"


def test_answers_and_briefings_cite_wikipedia_revisions(day) -> None:
    sources = [s for a in day["answers"] for s in a["sources"]]
    sources += [s for e in day["events"] if e["briefing"] for s in e["briefing"]["sources"]]
    for source in sources:
        if source["url"] is not None:
            assert source["url"].startswith("https://en.wikipedia.org/w/index.php?oldid=")
            assert source["url"].endswith(f"oldid={source['revision']}")
    for answer in day["answers"]:
        assert answer["correctness"], f"{answer['id']} has no judge scores"


def committed_files() -> list[Path]:
    return [f for f in SITE.rglob("*") if f.is_file() and VENDOR not in f.parents]


def test_the_committed_site_is_small() -> None:
    size = sum(f.stat().st_size for f in committed_files())
    assert size < MAX_SITE_BYTES, f"site/ without vendor/ is {size / 1e6:.1f} MB"


def test_the_vendored_files_stay_under_their_own_limit() -> None:
    """Pinned by hash, so their size is fixed; checked here when they are present."""
    if not VENDOR.exists():
        pytest.skip("site/vendor/ not vendored here (python scripts/vendor_relate.py)")
    assert vendor_relate.problems() == []
    size = vendor_relate.vendored_bytes()
    assert size < vendor_relate.MAX_VENDOR_BYTES, f"site/vendor/ is {size / 1e6:.1f} MB"


def test_the_vendored_files_are_never_committed() -> None:
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "site/vendor/" in ignore


# --- Privacy --------------------------------------------------------------------------------
def test_no_personal_fields_are_exported(day) -> None:
    keys = {key for _, key, _ in walk(day)}
    assert not keys & PERSONAL_KEYS, keys & PERSONAL_KEYS
    for path, key, value in walk(day):
        if key == "editor_count":
            assert isinstance(value, int), path  # a count, never names


def test_no_addresses_or_user_pages_in_the_text(day) -> None:
    for text in strings(day):
        assert not IPV4.search(text), text
        assert not IPV6.search(text), text
        assert not re.search(r"\bUser(?: talk)?:", text), text


def test_the_export_keeps_only_declared_columns() -> None:
    """Personal columns a query might return never reach the output."""
    moment = datetime(2026, 11, 5, 16, 0, tzinfo=UTC)
    member = {
        "event_number": 1,
        "title": "Storm Amy",
        "wikidata_id": "Q1",
        "description": "storm",
        "started_at": moment,
        "ended_at": None,
        "total_edits": 7,
        "distinct_editors": 3,
        "score": 4.0,
        "user": "Someone",
        "comment": "an edit summary",
        "correlation_id": "abc",
    }
    row = {
        "number": 1,
        "title": "Storm Amy",
        "started_at": moment,
        "ended_at": None,
        "score": 4.0,
        "spike_run_id": "r",
    }
    pg = {"events": [row], "members": [member], "model": None}
    graph = {"events": [], "entities": [], "relations": []}
    export = export_demo.build_day(moment.date(), "run", pg, graph, [])
    text = export_demo.to_json(export)
    for leaked in ("Someone", "an edit summary", "abc", '"user"', '"comment"'):
        assert leaked not in text


# --- Building the export (pure functions) ---------------------------------------------------
def test_pages_join_in_order_with_the_revision_the_model_read() -> None:
    t = [datetime(2026, 11, 5, h, 0, tzinfo=UTC) for h in (16, 17, 18)]
    members = [
        {"title": "A", "started_at": t[0], "ended_at": t[1], "total_edits": 5,
         "distinct_editors": 3, "score": 4.123},
        {"title": "B", "started_at": t[1], "ended_at": None, "total_edits": 6,
         "distinct_editors": 4, "score": 5.0},
        {"title": "A", "started_at": t[2], "ended_at": None, "total_edits": 8,
         "distinct_editors": 5, "score": 6.0},
    ]  # fmt: skip
    pages = export_demo.build_pages(members, {"B": 42})
    assert [p.title for p in pages] == ["A", "B"]
    assert len(pages[0].spikes) == 2 and pages[0].spikes[0].score == 4.12
    assert pages[1].revision == 42 and pages[0].revision is None
    assert pages[0].url == "https://en.wikipedia.org/wiki/A"


def test_a_briefing_is_split_from_its_sources() -> None:
    stored = {
        "briefing": "Text [1] and [2].\n\nSources:\n[1] X, revision 5: https://...\n[2] Y",
        "briefing_sources": ["X", "WikiPulse event r:1: title and description"],
        "briefing_urls": ["https://en.wikipedia.org/w/index.php?oldid=5", ""],
        "briefing_verified": True,
        "briefing_model": "ollama:qwen3:8b",
    }
    briefing = export_demo.build_briefing(stored)
    assert briefing.text == "Text [1] and [2]."
    assert briefing.sources[0].revision == 5
    assert briefing.sources[1].url is None
    assert export_demo.build_briefing({"briefing": None}) is None


def test_citations_become_permanent_links() -> None:
    source = export_demo.parse_citation("Ueno Bank, revision 1383953174")
    assert source.title == "Ueno Bank" and source.revision == 1383953174
    assert source.url == "https://en.wikipedia.org/w/index.php?oldid=1383953174"
    assert export_demo.parse_citation("Something else").url is None


# --- No external origins --------------------------------------------------------------------
def absolute_urls(text: str) -> list[str]:
    return re.findall(r"(?:https?:)?//[^\s\"'`)<>]+", text)


def test_the_page_loads_nothing_from_another_origin() -> None:
    html = (SITE / "index.html").read_text(encoding="utf-8")
    # Every resource the page loads (scripts, styles, icons, images, frames) is relative.
    for tag, url in LOADING_TAG.findall(html):
        assert not re.match(r"^(?:[a-z]+:|//)", url), f"<{tag}> loads {url}"
    # Absolute URLs appear only as links a visitor may click.
    for url in absolute_urls(html):
        assert url.startswith(LINK_PREFIXES), url


def test_the_page_declares_a_same_origin_content_security_policy() -> None:
    html = (SITE / "index.html").read_text(encoding="utf-8")
    match = re.search(r'http-equiv="Content-Security-Policy"\s+content="([^"]+)"', html)
    assert match, "no Content-Security-Policy"
    policy = match.group(1)
    assert "default-src 'none'" in policy
    for directive in ("script-src", "style-src", "connect-src", "img-src"):
        assert f"{directive} 'self'" in policy
    # Spec 13a: WebAssembly needs 'wasm-unsafe-eval' in script-src, and nothing else
    # is loosened (no 'unsafe-eval', no 'unsafe-inline', no other origin).
    directives = dict(d.strip().split(" ", 1) for d in policy.split(";") if d.strip())
    assert directives["script-src"] == "'self' 'wasm-unsafe-eval'"
    rest = policy.replace("'wasm-unsafe-eval'", "")
    assert "http" not in rest and "*" not in rest and "unsafe" not in rest
    assert "worker-src" not in policy  # workers fall back to script-src 'self'


def test_scripts_and_styles_reach_no_other_origin() -> None:
    assert {s.name for s in SCRIPTS} >= {"app.js", "explore.js", "graph-worker.js"}
    for path in SCRIPTS:
        script = path.read_text(encoding="utf-8")
        for url in absolute_urls(script):
            assert url.startswith(LINK_PREFIXES) or url == SVG_NAMESPACE, (path.name, url)
        for call in LOADING_CALL.findall(script):
            assert "http" not in call and "//" not in call, (path.name, call)
        for source in re.findall(r"^import .* from \"([^\"]+)\"", script, re.MULTILINE):
            assert source.startswith("./"), (path.name, source)
    app = (SITE / "app.js").read_text(encoding="utf-8")
    assert 'DATA_URL = "data/day.json"' in app  # relative: works under /wikipulse/
    worker = (SITE / "graph-worker.js").read_text(encoding="utf-8")
    assert 'GRAPH_URL = "data/graph.json"' in worker
    assert 'from "./vendor/relate/relate.js"' in worker  # the .wasm comes from this site
    for css in SITE.rglob("*.css"):
        text = css.read_text(encoding="utf-8")
        assert "url(" not in text and "@import" not in text, css
    for svg in SITE.rglob("*.svg"):
        text = svg.read_text(encoding="utf-8")
        assert all(u == SVG_NAMESPACE for u in absolute_urls(text)), svg
        assert "<script" not in text and "href" not in text, svg


def test_the_site_needs_no_build_step() -> None:
    names = {f.name for f in SITE.rglob("*") if f.is_file()}
    assert {"index.html", "app.js", "style.css", "day.json"} <= names
    assert {"explore.js", "graph-worker.js", "graph-db.js", "cypher-check.js"} <= names
    assert "graph.json" in names
    assert not names & {"package.json", "package-lock.json", "node_modules"}


def test_the_replay_does_not_load_the_database() -> None:
    """Only the graph section's button starts the worker, which alone loads Relate."""
    html = (SITE / "index.html").read_text(encoding="utf-8")
    assert "vendor/" not in re.sub(r'<a href="vendor/relate/LICENSE">', "", html)
    assert "graph-worker" not in html and ".wasm" not in html and "graph.json" not in html
    app = (SITE / "app.js").read_text(encoding="utf-8")
    assert "Worker" not in app and "graph" not in re.findall(r"fetch\(([^)]*)\)", app)[0]
    for path in SCRIPTS:
        text = re.sub(r"^\s*//.*$", "", path.read_text(encoding="utf-8"), flags=re.MULTILINE)
        if path.name != "graph-worker.js":
            assert "vendor/relate" not in text and "graph.json" not in text, path.name
    explore = (SITE / "explore.js").read_text(encoding="utf-8")
    assert explore.count("new Worker(") == 1
    start = explore.index("async start()")
    assert explore.index("new Worker(") > start  # created in start(), never at load
    assert '$("graph-start").addEventListener("click", openExplorer)' in explore

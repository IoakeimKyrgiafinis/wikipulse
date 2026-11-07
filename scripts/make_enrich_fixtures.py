"""Save real API responses for the enrichment tests.

Run: `uv run python scripts/make_enrich_fixtures.py`.

Enriches a few chosen titles against the real Wikimedia APIs (politely, with the
User-Agent from config) and writes every response to `tests/fixtures/enrich/<name>.json`,
so the unit tests can replay them without the network. Only response bodies are saved,
never request headers (the User-Agent holds a contact address).

Usage: make_enrich_fixtures.py [NAME=TITLE ...]   (default: the set the tests use)
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

import httpx

from wikipulse.config import get_settings
from wikipulse.enrich.cache import MemoryCache
from wikipulse.enrich.client import WikimediaClient
from wikipulse.enrich.pages import enrich
from wikipulse.logging_setup import setup_logging

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "enrich"

DEFAULT_TITLES = {
    "ok": "Syed Soharwardy",  # a small, ordinary article
    "redirect": "Susan Wiles",  # redirects to "Susie Wiles"
    "normalized": "frozen yogurt",  # lower-case first letter, normalized by the API
    "disambiguation": "Shipley",
    "missing": "WikiPulse test page that does not exist",
    "invalid": "Invalid [title]",  # square brackets are not allowed in titles
    "slash": "OS/2",  # a `/` that must be escaped in the REST URL
    "continued": "Stephen Miller",  # more than 500 links: needs `continue` requests
}


class RecordingTransport(httpx.AsyncBaseTransport):
    """Passes requests to the network and keeps a copy of every response."""

    def __init__(self) -> None:
        self.inner = httpx.AsyncHTTPTransport()
        self.exchanges: list[dict] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        content = await response.aread()
        try:
            body = json.loads(content) if content else None
        except ValueError:  # e.g. an HTTP redirect with an HTML body
            body = None
        self.exchanges.append(
            {
                "url": str(request.url.copy_with(query=None)),
                "params": dict(request.url.params),
                "status": response.status_code,
                "location": response.headers.get("Location"),
                "body": body,
            }
        )
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()


async def record(name: str, title: str) -> None:
    settings = get_settings()
    transport = RecordingTransport()
    async with WikimediaClient(
        settings.user_agent, MemoryCache(), concurrency=1, min_interval_s=1.0, transport=transport
    ) as client:
        info = await enrich(title, client, settings.wiki_api_url, settings.wiki_rest_url)
    path = FIXTURES / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(transport.exchanges, ensure_ascii=False, indent=1) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")
    print(
        f"{name}: {title!r} -> {info.title!r} status={info.status} redirect={info.is_redirect} "
        f"wikidata={info.wikidata_id} links={len(info.links)} categories={len(info.categories)} "
        f"requests={len(transport.exchanges)}"
    )


async def main(argv: list[str]) -> int:
    setup_logging()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    titles = dict(arg.split("=", 1) for arg in argv) if argv else DEFAULT_TITLES
    for name, title in titles.items():
        await record(name, title)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))

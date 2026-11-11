"""An offline stand-in for the Wikimedia APIs: `uv run python scripts/fake_wikimedia.py`.

For sessions that must not contact Wikimedia (spec 09b: the contact address in .env is
still a placeholder). Point the graph runner at it instead of the real APIs:

    WIKI_API_URL=http://host.docker.internal:8089/w/api.php
    WIKIDATA_API_URL=http://host.docker.internal:8089/wikidata/api.php

Every request gets an empty, well-formed answer: no revisions during the burst (so the
extraction sees the page summaries already in Postgres, not the text added), no
pageprops, no Wikidata search results (so entities stay unlinked). It never forwards
anything. Each request is logged, which shows what a real run would have asked.
"""

import argparse
import json
import logging
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger("fake_wikimedia")


def answer(path: str, params: dict[str, list[str]]) -> dict[str, Any]:
    """The empty answer for one request, shaped like the real API's."""
    action = params.get("action", [""])[0]
    if action == "wbsearchentities":
        return {"search": [], "success": 1}
    if action == "compare":
        return {"compare": {"body": ""}}
    titles = params.get("titles", [""])[0].split("|")
    return {"batchcomplete": True, "query": {"pages": [{"title": t} for t in titles if t]}}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (the http.server name)
        url = urlsplit(self.path)
        params = parse_qs(url.query)
        body = json.dumps(answer(url.path, params)).encode()
        logger.info("%s %s", url.path, {k: v[0] for k, v in params.items()})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # the request is logged above
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="0.0.0.0", help="0.0.0.0: reachable from kind")
    parser.add_argument("--port", type=int, default=8089)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    logger.info("Fake Wikimedia on %s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

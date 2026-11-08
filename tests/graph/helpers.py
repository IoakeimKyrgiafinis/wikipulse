"""Shared pieces of the graph tests: an event page, saved Wikimedia answers, a fake LLM."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from tests.enrich.helpers import request_id
from wikipulse.graph.inputs import EventInput, EventPage

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "graph"
API_URL = "https://en.wikipedia.org/w/api.php"
WIKIDATA_URL = "https://www.wikidata.org/w/api.php"
T0 = datetime(2026, 11, 5, 16, 0, tzinfo=UTC)


def saff_page(**changes: Any) -> EventPage:
    """The page of the saved revisions: its burst ran from 15:51:54 to 16:02:04 UTC."""
    values: dict[str, Any] = {
        "title": "2026 SAFF Championship",
        "spike_titles": ("2026 SAFF Championship",),
        "wikidata_id": "Q131753497",
        "description": "Fifteenth edition of the SAFF Championship",
        "summary": "The 2026 SAFF Championship will be the 15th edition of the SAFF "
        "Championship, organized by the South Asian Football Federation (SAFF).",
        "links": (
            "Bangladesh",
            "Bangladesh Football Federation",
            "South Asian Football Federation",
        ),
        "first_edit_at": datetime(2026, 11, 5, 15, 51, 54, tzinfo=UTC),
        "last_edit_at": datetime(2026, 11, 5, 16, 2, 4, tzinfo=UTC),
        "score": 7.0,
    }
    return EventPage(**(values | changes))


def event(pages: tuple[EventPage, ...] | None = None, **changes: Any) -> EventInput:
    values: dict[str, Any] = {
        "run_id": "test-run",
        "number": 7,
        "status": "ended",
        "title": "2026 SAFF Championship",
        "started_at": T0,
        "ended_at": None,
        "fingerprint": "1,2",
        "pages": pages if pages is not None else (saff_page(),),
    }
    return EventInput(**(values | changes))


class SavedWikimedia(httpx.MockTransport):
    """Answers requests from tests/fixtures/graph/wikimedia.json and records them."""

    def __init__(self) -> None:
        self.responses = {}
        for exchange in json.loads((FIXTURES / "wikimedia.json").read_text(encoding="utf-8")):
            key = request_id(exchange["url"], exchange["params"])
            self.responses[key] = (exchange["status"], exchange["body"])
        self.requests: list[httpx.Request] = []
        super().__init__(self.answer)

    def answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = request_id(str(request.url.copy_with(query=None)), dict(request.url.params))
        if key not in self.responses:
            raise AssertionError(f"No saved response for {request.url}")
        status, body = self.responses[key]
        return httpx.Response(status, json=body)

    def actions(self) -> list[str]:
        """The `action` (and `prop`) of every request so far, in order."""
        return [
            "/".join(filter(None, (r.url.params.get("action"), r.url.params.get("prop"))))
            for r in self.requests
        ]


def answer(entities: list[dict], relations: list[dict] | None = None, **extra: Any) -> str:
    """A model answer as JSON text."""
    return json.dumps(
        {
            "title": extra.get("title", "Bangladesh beat Bhutan at the SAFF Championship"),
            "description": extra.get(
                "description", "Bangladesh beat Bhutan 1-0 in the 2026 SAFF Championship."
            ),
            "entities": entities,
            "relations": relations or [],
        }
    )


def entity(name: str, type_: str = "organization", source: int = 1, certainty: str = "high"):
    return {"name": name, "type": type_, "source": source, "certainty": certainty}


def relation(subject: str, rel: str, object_: str, source: int = 1, certainty: str = "high"):
    return {
        "subject": subject,
        "relation": rel,
        "object": object_,
        "source": source,
        "certainty": certainty,
    }

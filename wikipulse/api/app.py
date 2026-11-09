"""The WikiPulse web API and live page (FastAPI).

| Route | What it does |
|-------|--------------|
| `GET /` | the live page (`static/index.html`) |
| `GET /events` | recent events of the run, active first, then by latest spike |
| `GET /events/{event_id}` | one event: pages, entities, relations, briefing |
| `POST /ask` | a cited GraphRAG answer (`Explorer.ask`, i.e. `wikipulse.qa`) |
| `GET /health` | whether Postgres and Neo4j answer, and how many live clients there are |
| `WS /ws/events` | a snapshot on connect, then every new or changed event |
| `GET /docs` | the API documentation FastAPI generates from the code |

`create_app()` takes its parts ready-made (tests pass fakes); `build_app()` makes them
from the settings. Start it with `uv run python -m wikipulse.api`.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import anyio
import psycopg
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.websockets import WebSocket, WebSocketDisconnect

from wikipulse.api.events import ANY_STATUS, MAX_LIST, EventReader, PostgresEvents
from wikipulse.api.live import CLOSE, Broadcaster, Client, EventWatcher
from wikipulse.api.models import AskRequest, AskResult, EventDetailOut, EventList, Health
from wikipulse.config import Settings
from wikipulse.llm.errors import LLMError
from wikipulse.mcp_server.explore import UNREACHABLE, Explorer, ToolInputError

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")
MAX_CLIENT_MESSAGE = 1000  # characters; clients only ever send "ping"
MAX_BODY_BYTES = 8 * 1024  # a request body; a 500-character question needs about 2 KB
# The page loads nothing from outside: scripts, styles and connections are same-origin.
PAGE_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
GRAPH_DOWN = "The knowledge graph (Neo4j) is not reachable. Start it with `docker compose up -d`."
POSTGRES_DOWN = "Postgres is not reachable. Start it with `docker compose up -d`."


class BodyLimit:
    """Refuses request bodies over `limit` bytes before anything reads them (HTTP 413).
    A body without a declared length (chunked) is refused too (HTTP 411): browsers and
    HTTP clients always declare it for a JSON body."""

    def __init__(self, app, limit: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope["method"] in ("POST", "PUT", "PATCH"):
            headers = dict(scope["headers"])
            length = headers.get(b"content-length")
            if length is None or not length.isdigit():
                await _refuse(scope, receive, send, 411, "Send the body with a Content-Length.")
                return
            if int(length) > self.limit:
                detail = f"The request body is over {self.limit} bytes."
                await _refuse(scope, receive, send, 413, detail)
                return
        await self.app(scope, receive, send)


async def _refuse(scope, receive, send, status: int, detail: str) -> None:
    await JSONResponse({"detail": detail}, status_code=status)(scope, receive, send)


class QuestionGate:
    """Questions share one model on one GPU, so `LazyAsker` runs them one at a time.
    This counts the questions running or waiting and turns away the ones beyond `limit`."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.in_flight = 0

    def try_enter(self) -> bool:
        if self.in_flight >= self.limit:
            return False
        self.in_flight += 1
        return True

    def leave(self) -> None:
        self.in_flight -= 1


def create_app(
    reader: EventReader,
    explorer: Explorer,
    *,
    poll_interval_s: float = 5,
    watch_limit: int = 200,
    max_waiting_questions: int = 3,
    allowed_hosts: Sequence[str] = LOOPBACK_HOSTS,
    close_on_shutdown: bool = True,
) -> FastAPI:
    """The API over ready-made parts."""
    broadcaster = Broadcaster()
    watcher = EventWatcher(
        lambda: reader.cards(watch_limit, ANY_STATUS), broadcaster, poll_interval_s
    )
    gate = QuestionGate(max_waiting_questions)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(watcher.run(), name="event-watcher")
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            if close_on_shutdown:
                explorer.close()

    app = FastAPI(
        title="WikiPulse",
        summary="Breaking news detected from Wikipedia's live edits, with a knowledge graph.",
        description=(
            "Events are pages that suddenly attract unusual editing, grouped into stories. "
            "Times are event time (from the edits), not the wall clock. Facts cite the "
            "Wikipedia revision they were read from. Live updates: WebSocket `/ws/events`."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.reader = reader
    app.state.watcher = watcher
    app.state.broadcaster = broadcaster
    app.state.gate = gate
    # Only requests addressed to this machine: a web page elsewhere can't reach the API by
    # pointing a domain name at 127.0.0.1 (DNS rebinding).
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))
    app.add_middleware(BodyLimit)

    @app.middleware("http")
    async def page_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers.update(PAGE_SECURITY_HEADERS)
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        """FastAPI's 422 without echoing the input back (it may be long)."""
        errors = [{k: v for k, v in e.items() if k != "input"} for e in error.errors()]
        return JSONResponse({"detail": jsonable_encoder(errors)}, status_code=422)

    @app.exception_handler(psycopg.OperationalError)
    async def postgres_down(request: Request, error: psycopg.OperationalError) -> JSONResponse:
        logger.warning("Postgres is not reachable: %s", error)
        return JSONResponse({"detail": POSTGRES_DOWN}, status_code=503)

    _add_routes(app, reader, explorer, gate)
    _add_websocket(app, watcher, broadcaster, allowed_hosts)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def _add_routes(app: FastAPI, reader: EventReader, explorer: Explorer, gate: QuestionGate):
    @app.get("/", include_in_schema=False)
    def page() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/events", tags=["events"])
    def list_events(
        limit: Annotated[int, Query(ge=1, le=MAX_LIST, description="Most events.")] = 50,
        status: Annotated[
            Literal["all", "active", "ended"], Query(description="Which events.")
        ] = "all",
        min_pages: Annotated[
            int, Query(ge=1, le=100, description="Only events with at least this many pages.")
        ] = 1,
    ) -> EventList:
        """Recent events of the run: active ones first, then by their latest spike."""
        return reader.listing(limit, status, min_pages)

    @app.get("/events/{event_id}", tags=["events"], responses={404: {}, 422: {}})
    def get_event(
        event_id: Annotated[
            str, PathParam(max_length=120, description="'run:number', or just the number.")
        ],
    ) -> EventDetailOut:
        """One event in full: its pages, the entities and relations the knowledge graph
        holds for it, and the analyst's briefing with its sources."""
        try:
            detail = reader.detail(event_id)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        if detail is None:
            raise HTTPException(404, f"There is no event {event_id!r}.")
        return detail

    @app.post("/ask", tags=["questions"], responses={422: {}, 429: {}, 503: {}})
    async def ask(body: AskRequest) -> AskResult:
        """Answer a question from the knowledge graph (GraphRAG) with numbered citations.
        Slow: 5 to 60 seconds. Questions run one at a time."""
        if not gate.try_enter():
            raise HTTPException(429, "Too many questions waiting. Try again in a minute.")
        try:
            return await anyio.to_thread.run_sync(explorer.ask, body.question)
        except ToolInputError as error:
            raise HTTPException(422, str(error)) from error
        except UNREACHABLE as error:
            logger.warning("Neo4j is not reachable: %s", error)
            raise HTTPException(503, GRAPH_DOWN) from error
        except LLMError as error:
            logger.warning("The answering model failed: %s", error)
            raise HTTPException(503, f"The answering model failed: {error}") from error
        finally:
            gate.leave()

    @app.get("/health", tags=["service"])
    async def health() -> Health:
        """Whether the API's services answer. Always 200 while the API itself runs."""
        postgres, neo4j = await anyio.to_thread.run_sync(reader.health)
        return Health(
            status="ok" if postgres and neo4j else "degraded",
            postgres=postgres,
            neo4j=neo4j,
            run_id=reader.run_id,
            websocket_clients=app.state.broadcaster.count,
            last_poll_ok=app.state.watcher.last_poll_ok,
        )


def origin_allowed(origin: str | None, allowed_hosts: Sequence[str]) -> bool:
    """Browsers send an Origin with every WebSocket; only pages served from an allowed
    host may connect. Clients that aren't browsers send none."""
    if origin is None:
        return True
    host = urlsplit(origin).hostname or ""
    return host in allowed_hosts or f"[{host}]" in allowed_hosts


def _add_websocket(
    app: FastAPI, watcher: EventWatcher, broadcaster: Broadcaster, allowed_hosts: Sequence[str]
) -> None:
    @app.websocket("/ws/events")
    async def events_socket(websocket: WebSocket) -> None:
        if not origin_allowed(websocket.headers.get("origin"), allowed_hosts):
            await websocket.close(code=1008)  # policy violation
            return
        await websocket.accept()
        client, snapshot = watcher.subscribe()
        try:
            await websocket.send_json(snapshot)
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(_send_loop, websocket, client, tasks.cancel_scope)
                tasks.start_soon(_receive_loop, websocket, client, tasks.cancel_scope)
        except WebSocketDisconnect:
            pass
        finally:
            broadcaster.unsubscribe(client)  # requirement 3: no dead clients left behind
            logger.debug("WebSocket client left (%d connected)", broadcaster.count)


async def _send_loop(websocket: WebSocket, client: Client, scope: anyio.CancelScope) -> None:
    try:
        while True:
            message = await client.queue.get()
            if message is CLOSE:
                await websocket.close(code=1013)  # try again later
                break
            await websocket.send_json(message)
    except (WebSocketDisconnect, RuntimeError, OSError):
        pass  # the client is gone
    finally:
        scope.cancel()


async def _receive_loop(websocket: WebSocket, client: Client, scope: anyio.CancelScope) -> None:
    """Reading is how a disconnect is noticed even when there is nothing to send."""
    try:
        while True:
            text = await websocket.receive_text()
            if text[:MAX_CLIENT_MESSAGE].strip().lower() == "ping":
                with suppress(asyncio.QueueFull):
                    client.queue.put_nowait({"type": "pong"})
    except (WebSocketDisconnect, RuntimeError, KeyError):
        pass
    finally:
        scope.cancel()


def build_app(settings: Settings) -> FastAPI:
    """The API on the real Postgres, Neo4j and LLM, as configured."""
    explorer = Explorer.from_settings(settings)
    reader = EventReader(
        PostgresEvents(settings.postgres_dsn), explorer.graph, explorer, settings.api_run_id
    )
    return create_app(
        reader,
        explorer,
        poll_interval_s=settings.api_poll_interval_s,
        watch_limit=settings.api_watch_limit,
        max_waiting_questions=settings.api_max_waiting_questions,
    )

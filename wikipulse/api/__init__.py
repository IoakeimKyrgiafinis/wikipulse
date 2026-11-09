"""The WikiPulse web API and live page (stage 5, spec 05e).

    uv run python -m wikipulse.api                      # http://127.0.0.1:8000
    uv run python -m wikipulse.api --run-id 03b-default-fix

`app.py` has the routes and the WebSocket, `events.py` joins grouping (Postgres) with the
knowledge graph (Neo4j), `live.py` pushes changes to connected browsers, and
`static/` holds the page (plain HTML, CSS and JavaScript; nothing loaded from outside).
"""

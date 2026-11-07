# Spec 05e: API and live web page

Status: ready
Stage: 5. GraphRAG, MCP, agent
Owner: agent
Depends on: 05a, 05d

## Goal
A backend API and a simple web page where events appear live as they're detected, each with its pages, entities and briefing, plus a question box.

## What you'll learn
- **Backend API design** with FastAPI: endpoints, validation, and auto-generated API docs at `/docs`.
- **Real-time web:** WebSockets pushing events to the browser as they happen.
- **Connecting a pipeline to users:** where the data system meets a product.

## Outputs
- `wikipulse/api/app.py`: `GET /events`, `GET /events/{id}`, `POST /ask`, `GET /health`, and a WebSocket `/ws/events` pushing new and updated events
- `wikipulse/api/static/index.html`: one page with a live event list, event details, and the question box (plain HTML and JavaScript, no build step)
- `uv run python -m wikipulse.api`

## Requirements
1. The page works with no internet beyond your own services.
2. Input validation and length limits on `/ask`.
3. WebSocket clients that disconnect are cleaned up.

## Acceptance criteria
- API tests with FastAPI's test client; a WebSocket test receiving a pushed event.
- During a live run, events appear on the page without refreshing.

## Look at it yourself (optional, never blocking)
- Use the page during a live run for 15 minutes and note what's confusing.

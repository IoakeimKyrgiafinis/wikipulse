# WikiPulse

WikiPulse watches Wikipedia's live edit stream, detects topics that suddenly attract
unusual editing (usually breaking news), groups them into events, builds a knowledge graph
of the people, organizations and places involved, and answers questions about them.

This is an early skeleton; see `docs/ROADMAP.md` for the plan.

## Setup

1. Install [Git](https://git-scm.com/), [uv](https://docs.astral.sh/uv/) and
   [Docker Desktop](https://www.docker.com/products/docker-desktop/).
2. Copy the example settings, then put your own contact address in `USER_AGENT`
   (Wikimedia requires it):
   - PowerShell: `Copy-Item .env.example .env`
   - bash: `cp .env.example .env`
3. Install Python and the dependencies: `uv sync`
4. Start Redis and Postgres: `docker compose up -d` (check with `docker compose ps`)
5. Check that the services are reachable: `uv run python -m wikipulse.check`
6. Run the tests: `uv run pytest`

Lint and format with `uv run ruff check .` and `uv run ruff format .`.

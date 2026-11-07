# AGENTS.md: WikiPulse

Instructions for coding agents working in this repository. Read this whole file before starting any task.

## What this project is

WikiPulse watches Wikipedia's live edit stream, detects topics that suddenly attract unusual editing (usually breaking news), groups them into events, builds a knowledge graph of the people, organizations and places involved, and answers questions about them with GraphRAG, through an API, a live web page and an MCP server. Around that sits a full MLOps platform: a model lifecycle, Kubernetes, Helm, Terraform, monitoring and CI/CD.

The human owner is learning alongside the build. Prefer clear, readable code over clever code. Every finished spec gets a learning note (see below), which is how the human follows what was built and why.

## Environment

- The human works on Windows 11 with the Claude desktop app. Commands you run use PowerShell.
- Docker Desktop runs the services (Redis, Postgres, and later others).
- CI runs on Linux (GitHub Actions), so all code must work on both Windows and Linux: use `pathlib`, never hard-coded path separators, and write helper scripts in Python, not shell.
- Line endings are normalized to LF by `.gitattributes`; don't change that file.
- The repository is hosted on GitHub.

## Stack

Current stages:
- Python 3.12 (async where it helps), dependencies managed with uv (`pyproject.toml`)
- Redis Streams for the event pipeline, Postgres 16 for detected spikes and events
- pydantic and pydantic-settings for data models and configuration
- pytest and ruff; GitHub Actions for CI

Later stages (don't add these before their spec says so): Neo4j, Ollama with local models, the official MCP Python SDK, FastAPI, MinIO, Airflow or Argo Workflows, MLflow, PyTorch, Kubernetes (kind or k3d), Helm, Terraform, Prometheus, Grafana, Loki, Langfuse.

Don't add a dependency the current spec doesn't allow. If you need one, explain why in your summary.

## Repository layout

```
wikipulse/        main package
  config.py       all settings (pydantic-settings)
  stream/         live stream consumer, recorder, replay (stage 1)
  detect/         spike detection (stage 2)
  llm/            the LLM client: neutral types, get_client(), backends (stage 4)
  eval/           evaluation code
tests/            pytest tests, mirroring wikipulse/
  fixtures/       small committed test data (e.g. recorded stream samples)
evals/            evaluation results and human labels
docs/
  ROADMAP.md      the overall plan
  specs/          one spec per task; your instructions live here
  reviews/        review reports
  learning/       one learning note per spec, written by agents
  devlog.md       the human's notes; don't edit
data/             recordings, caches, generated data; NEVER commit
```

Create folders as needed, but not for stages that haven't started.

## Commands

- Install: `uv sync`
- Start services: `docker compose up -d`
- Service check: `uv run python -m wikipulse.check`
- Tests: `uv run pytest` (unit tests only by default; `-m integration` needs Docker services; `-m live` touches the real Wikimedia services)
- Lint and format: `uv run ruff check .` and `uv run ruff format .`

If a command doesn't exist yet, say so rather than inventing a workaround.

## How to work on a task

1. You'll be pointed to a spec in `docs/specs/`. Read it fully, along with this file and any files it references.
2. Implement what the spec asks, choosing the best solution you can. Specs for later stages were written before earlier stages existed: adapt details to what the codebase actually contains, and if you find a clearly better design than the spec's, use it. Record every deviation and its reason in the learning note. Anything under "Out of scope" stays off-limits.
3. If the spec is ambiguous, choose the most reasonable interpretation, continue, and record it in the learning note. Stop only if the spec is impossible to carry out.
4. Run every acceptance criterion yourself. Never claim a criterion passes without running it.
5. Finish with a summary: what you built, how you verified each acceptance criterion, decisions you made, and anything unresolved.
6. Update the spec's `Status:` line to `done`, or to `blocked` with the reason.

## Rules

- **Never commit** anything under `data/`, the `.env` file, or any secret. Settings come from `wikipulse/config.py` and environment variables; document new ones in `.env.example`.
- **Be a polite client of Wikimedia.** Every request to Wikimedia services sends the descriptive User-Agent from config (with the human's contact address). Use one stream connection at a time, back off on errors, cache API responses, and never hammer an API.
- **Event time, not wall-clock time.** Processing logic (windows, baselines, spikes) uses the event's own timestamp, never `datetime.now()`. This makes a replayed recording produce exactly the same results as the live run.
- **Tests never touch the network.** Unit tests use committed fixtures and replays. Tests needing Docker services are marked `integration`; tests touching real Wikimedia services are marked `live`. Both are skipped by default.
- Labels in `evals/labels/` may be produced by an LLM labeler. Every record states its labeler (`llm` or `human`). Never overwrite human labels.
- Never weaken, skip or delete tests to make them pass.
- Don't refactor code outside the spec's scope.
- **All LLM calls go through `wikipulse.llm`** (`get_client(...).chat(...)`). Never import `ollama`, `anthropic` or another provider's package, or call a model's HTTP API, outside `wikipulse/llm/`.
- Keep functions small, with type hints on public functions. Log with the `logging` module, not `print`, inside the package.
- If an acceptance step would take more than 60 minutes, run it on a smaller sample instead and record that in the learning note. Don't stop the workflow for it.
- Don't install system-wide software silently. If a required tool (for example kind, kubectl, helm or terraform) is missing, install it with `winget` and say so in the learning note.

## Definition of done

Every acceptance criterion verified by running it, tests pass, ruff passes, learning note written, spec status updated.

## Learning notes

After each spec, write `docs/learning/<spec name>.md` in plain language for someone learning AI engineering:
- what was built, and how the pieces fit together,
- the key concepts from the spec's "What you'll learn" section, explained with this project's actual code (file and function names),
- design decisions and deviations from the spec, with reasons,
- the results (numbers from evaluations, where the spec has them),
- three questions to test understanding, with answers in a collapsed `<details>` block.

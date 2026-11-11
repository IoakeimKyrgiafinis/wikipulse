# Spec 10: Polish and presentation

Status: done
Stage: 10. Polish
Owner: agent
Depends on: everything

## Goal
Make WikiPulse easy to run, easy to understand and convincing to an employer.

## What you'll learn
- **Communicating technical work:** explaining architecture, trade-offs and results to different audiences. Often the deciding skill in hiring.
- **Developer experience:** a one-command setup someone else can actually follow.

## Outputs
- `scripts/quickstart.py`: starts the minimal system (Docker Compose, no Kubernetes), replays a bundled sample recording, and opens the web page, so it works even without a live connection
- Repo cleanup: dead code, stale TODOs, docstrings on public functions
- `docs/architecture.md` with a Mermaid diagram drafted from the code

## Also write
- **README:** what WikiPulse does (with a GIF), the architecture diagram, the tech stack, results (detection precision and recall, extraction accuracy, QA scores, model comparison), key design decisions and trade-offs, what you'd do with more resources, and how you built it with coding agents (link to `docs/specs/`).
- **Blog post drafts** (two or three) in `docs/blog/`, built from the learning notes.
- A demo video is left for the human (optional).
- Strong blog topics: designing detection with imperfect ground truth; making a small local model a usable agent; event time and replayable streaming.

## Acceptance criteria
- On a clean machine with Docker, uv and Ollama, the quickstart works (test it in a fresh clone).
- CI is green on the final commit.

# Spec 06b: Event-driven worker

Status: ready
Stage: 6. Data lake and batch
Owner: agent
Depends on: 06a

## Goal
When a new hourly file lands in storage, a worker reacts automatically and computes statistics: the same pattern as S3 triggering AWS Lambda, built locally.

## What you'll learn
- **Event-driven architecture:** components reacting to events instead of polling or schedules.
- **The serverless pattern** (small, stateless functions triggered by events), and how it maps to AWS Lambda.
- **Idempotent handlers:** the same notification delivered twice must not double-count.

## Outputs
- MinIO bucket notifications configured to publish "object created" events (to Redis or a webhook), set up by a script
- `wikipulse/workers/hourly_stats.py`: a small stateless handler computing per-hour statistics (edits, distinct pages, distinct editors, bot share, top pages) into a Postgres table `hourly_stats`
- `uv run python -m wikipulse.workers.run`

## Requirements
1. Handlers are idempotent (upsert by hour).
2. Failed handlers retry, then go to a dead-letter list for inspection.
3. A backfill command processes existing files that never got a notification.

## Acceptance criteria
- Unit tests for the handler; an integration test where writing a file triggers the stats row.
- A deliberately failing handler ends up in the dead-letter list.

## Look at it yourself (optional, never blocking)
- Explain in your devlog how this would look with S3, Lambda and a dead-letter queue on AWS.

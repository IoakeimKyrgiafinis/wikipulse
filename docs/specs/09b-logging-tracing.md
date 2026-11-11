# Spec 09b: Centralized logging and LLM tracing

Status: done
Stage: 9. Observability and CI/CD
Owner: agent
Depends on: 09a

## Goal
All services log to one searchable place, and every LLM call and agent step is traced.

## What you'll learn
- **Structured logging:** JSON logs with consistent fields (service, event ID, spike ID) that can be searched and joined.
- **Log aggregation** with Loki, the lighter alternative to the ELK stack, using the same ideas.
- **Tracing:** following one event through every service and LLM call, the fastest way to debug a pipeline.

## Outputs
- Structured JSON logging across services, with a correlation ID carried from edit to spike to event to briefing
- Loki and a log collector in the cluster (community Helm charts), connected to Grafana
- LLM and agent tracing with Langfuse (free cloud tier) when keys are set, a silent no-op otherwise

## Acceptance criteria
- In Grafana, searching one event's correlation ID shows its whole path across services.
- Tracing works with keys and does nothing without them (tested).

## Look at it yourself (optional, never blocking)
- Pick one wrong briefing and find the cause through logs and traces. Write up the debugging story.

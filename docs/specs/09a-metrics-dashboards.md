# Spec 09a: Metrics, dashboards and alerts

Status: ready
Stage: 9. Observability and CI/CD
Owner: agent
Depends on: 08b

## Goal
Every service exposes metrics; Prometheus collects them, Grafana shows them, and alerts fire when something goes wrong.

## What you'll learn
- **The metric types:** counters, gauges and histograms, and when each fits.
- **What to watch in a streaming system:** throughput, lag, error rates, and queue length.
- **Dashboards as code:** dashboards stored in the repo, not clicked together by hand.
- **Alert design:** alerting on symptoms users feel, not every blip. Read about "alert fatigue" first.

## Outputs
- Prometheus metrics in each service (`prometheus-client`): events per second, consumer lag, spikes started, LLM latency and tokens, extraction failures, model scores, drift values
- Prometheus and Grafana installed in the cluster through their community Helm charts
- `observability/dashboards/` (JSON, loaded automatically): pipeline health, detection, LLM usage, model health
- Alert rules: consumer lag above 5 minutes, no events for 2 minutes, extraction failure rate high, drift detected

## Acceptance criteria
- Dashboards show live data during a session.
- Stopping the consumer fires the "no events" alert.

## Look at it yourself (optional, never blocking)
- Sketch the dashboards you want before the agent builds them, then compare.

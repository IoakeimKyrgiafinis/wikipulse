"""Prometheus metrics (spec 09a).

- `catalog.py`: every WikiPulse metric, with its type and what it is for. The services
  update them where the work happens (the consumer in `Consumer.process`, the detector in
  `DetectionRunner`, the LLM client in `LLMClient._ask`, ...).
- `server.py`: `start_metrics_server(settings)` serves them on `METRICS_PORT`.
- `models.py`: the model health metrics read from Postgres (drift reports, stored scores),
  served by the API, since drift is checked by a weekly job that doesn't stay up.
"""

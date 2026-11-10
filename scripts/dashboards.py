"""Build the Grafana dashboards in observability/dashboards/ from code.

    uv run python scripts/dashboards.py          # write the JSON files
    uv run python scripts/dashboards.py --check  # exit 1 if a file is out of date

Dashboards as code: the JSON files are generated from the definitions below, and the
generated files are committed. Terraform puts each file into a ConfigMap, and Grafana's
sidecar loads it (infra/cluster/monitoring.tf). To change a dashboard, change this file
and run it; `tests/test_observability.py` fails if the JSON and this file disagree. A
panel built by hand in Grafana's UI can be copied here from its JSON ("Inspect > Panel
JSON"); the UI itself can't save over these dashboards (allowUiUpdates: false).

Why a generator instead of hand-written JSON: a Grafana dashboard is about 50 lines of
JSON per panel, mostly the same. Here a panel is one call (`stat(...)`, `timeseries(...)`)
and the layout, the data source and the defaults are written once.
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "observability" / "dashboards"
DATASOURCE = {"type": "prometheus", "uid": "prometheus"}  # the uid kube-prometheus-stack sets
WIDTH = 24  # Grafana's grid is 24 columns wide

GREEN, YELLOW, RED = "green", "yellow", "red"


@dataclass
class Query:
    expr: str
    legend: str = ""


def steps(*pairs: tuple[str, float | None]) -> dict[str, Any]:
    """Thresholds: (color, from value), the first one from minus infinity (None)."""
    return {
        "mode": "absolute",
        "steps": [{"color": color, "value": value} for color, value in pairs],
    }


@dataclass
class Panel:
    title: str
    kind: str  # timeseries, stat, bargauge
    queries: list[Query]
    description: str
    width: int = 12
    height: int = 8
    unit: str = "short"
    thresholds: dict[str, Any] | None = None
    mappings: list[dict[str, Any]] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)
    decimals: int | None = None

    def to_json(self, panel_id: int, x: int, y: int) -> dict[str, Any]:
        defaults: dict[str, Any] = {"unit": self.unit, "mappings": self.mappings}
        defaults["thresholds"] = self.thresholds or steps((GREEN, None))
        if self.decimals is not None:
            defaults["decimals"] = self.decimals
        if self.kind == "timeseries":
            defaults["custom"] = {"lineWidth": 2, "fillOpacity": 10, "spanNulls": False}
        return {
            "id": panel_id,
            "type": self.kind,
            "title": self.title,
            "description": self.description,
            "datasource": DATASOURCE,
            "gridPos": {"x": x, "y": y, "w": self.width, "h": self.height},
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": self.options,
            "targets": [
                {
                    "refId": chr(ord("A") + i),
                    "datasource": DATASOURCE,
                    "expr": q.expr,
                    "legendFormat": q.legend,
                    "range": True,
                }
                for i, q in enumerate(self.queries)
            ],
        }


def stat(title: str, expr: str, description: str, legend: str = "", **kw: Any) -> Panel:
    options = {
        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        "colorMode": "background",
        "graphMode": "area",
        "textMode": "value_and_name" if legend else "value",
    }
    kw.setdefault("width", 6)
    kw.setdefault("height", 5)
    return Panel(title, "stat", [Query(expr, legend)], description, options=options, **kw)


def timeseries(title: str, queries: list[Query], description: str, **kw: Any) -> Panel:
    options = {
        "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
        "tooltip": {"mode": "multi", "sort": "desc"},
    }
    return Panel(title, "timeseries", queries, description, options=options, **kw)


def bars(title: str, queries: list[Query], description: str, **kw: Any) -> Panel:
    options = {
        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        "orientation": "horizontal",
        "displayMode": "gradient",
        "showUnfilled": True,
    }
    return Panel(title, "bargauge", queries, description, options=options, **kw)


def layout(panels: list[Panel]) -> list[dict[str, Any]]:
    """Left to right, then the next row, like text."""
    result, x, y, row_height = [], 0, 0, 0
    for number, panel in enumerate(panels, start=1):
        if x + panel.width > WIDTH:
            x, y, row_height = 0, y + row_height, 0
        result.append(panel.to_json(number, x, y))
        x += panel.width
        row_height = max(row_height, panel.height)
    return result


def dashboard(uid: str, title: str, description: str, panels: list[Panel]) -> dict[str, Any]:
    return {
        "uid": uid,
        "title": title,
        "description": description,
        "tags": ["wikipulse"],
        "timezone": "utc",
        "editable": False,
        "graphTooltip": 1,  # a shared crosshair across panels
        "refresh": "30s",
        "time": {"from": "now-1h", "to": "now"},
        "schemaVersion": 39,
        "version": 1,
        "templating": {"list": []},
        "annotations": {"list": []},
        "panels": layout(panels),
    }


UP_DOWN = [
    {
        "type": "value",
        "options": {
            "0": {"text": "down", "color": RED, "index": 0},
            "1": {"text": "up", "color": GREEN, "index": 1},
        },
    }
]
OK_DRIFT = [
    {
        "type": "value",
        "options": {
            "0": {"text": "ok", "color": GREEN, "index": 0},
            "1": {"text": "drift", "color": RED, "index": 1},
        },
    }
]
WRITER_LAG = "max(wikipulse_consumer_lag_seconds)"
DETECTOR_LAG = (
    "max(wikipulse_consumer_last_event_timestamp_seconds)"
    " - max(wikipulse_detector_last_event_timestamp_seconds)"
)
LAG_COLORS = steps((GREEN, None), (YELLOW, 60), (RED, 300))


def pipeline_health() -> dict[str, Any]:
    panels = [
        stat(
            "Edits written per second",
            'sum(rate(wikipulse_consumer_messages_total{outcome="kept"}[1m]))',
            "English Wikipedia article edits the consumer (or a replay) wrote to the stream.",
            unit="suffix:/s",
            decimals=2,
            thresholds=steps((RED, None), (GREEN, 0.01)),
        ),
        stat(
            "Stream writer lag",
            WRITER_LAG,
            "How old the newest written edit is. Live: by the wall clock. A replay: by its "
            "own event-time clock. Alert above 5 minutes.",
            unit="s",
            thresholds=LAG_COLORS,
        ),
        stat(
            "Detector behind the writer",
            DETECTOR_LAG,
            "Event time between the newest written edit and the newest edit the detector "
            "processed. Alert above 5 minutes.",
            unit="s",
            thresholds=LAG_COLORS,
        ),
        stat(
            "Alerts firing",
            'count(ALERTS{alertstate="firing"}) or vector(0)',
            "Prometheus alerts firing now (WikiPulse rules). See Alerts in Prometheus.",
            thresholds=steps((GREEN, None), (RED, 1)),
        ),
        timeseries(
            "Messages per second, by outcome",
            [
                Query(
                    "sum by (outcome) (rate(wikipulse_consumer_messages_total[1m]))",
                    "{{outcome}}",
                )
            ],
            "Throughput of the stream writer: kept (written), discarded (other wikis, "
            "bots' pages, non-articles), duplicates, malformed.",
            unit="suffix:/s",
        ),
        timeseries(
            "Lag",
            [Query(WRITER_LAG, "stream writer"), Query(DETECTOR_LAG, "detector")],
            "The writer's lag and the detector's distance behind it (event time).",
            unit="s",
        ),
        timeseries(
            "Queue in front of the detector",
            [
                Query("max(wikipulse_detector_stream_lag_entries)", "not yet delivered"),
                Query("max(wikipulse_detector_pending_entries)", "delivered, not acknowledged"),
            ],
            "Redis consumer group of the detector: entries waiting, and entries read but not "
            "yet acknowledged (acknowledged at every snapshot).",
        ),
        timeseries(
            "Wikimedia API answers, by status",
            [Query("sum by (status) (rate(wikipulse_wiki_api_requests_total[5m]))", "{{status}}")],
            "Requests of the enricher and the graph runner. 429 = Wikimedia asks us to slow "
            "down; cache = answered without a request.",
            unit="suffix:/s",
        ),
        stat(
            "Services",
            "max by (service) (up{service!=''})",
            "Whether Prometheus can scrape each WikiPulse process.",
            legend="{{service}}",
            width=12,
            mappings=UP_DOWN,
            thresholds=steps((RED, None), (GREEN, 1)),
        ),
        timeseries(
            "Reconnects to Wikimedia",
            [Query("sum(increase(wikipulse_consumer_reconnects_total[10m]))", "per 10 min")],
            "The live consumer lost the stream and connected again (with backoff).",
        ),
        timeseries(
            "Memory per service",
            [Query("max by (service) (process_resident_memory_bytes{service!=''})", "{{service}}")],
            "Resident memory of each process (compare with the pod's limit in values.yaml).",
            unit="bytes",
        ),
        timeseries(
            "CPU per service",
            [
                Query(
                    "sum by (service) (rate(process_cpu_seconds_total{service!=''}[1m]))",
                    "{{service}}",
                )
            ],
            "CPU cores used by each process.",
            unit="short",
        ),
    ]
    return dashboard(
        "wikipulse-pipeline",
        "WikiPulse: Pipeline health",
        "Throughput, lag, queues and errors of the edit stream (stages 1 to 3).",
        panels,
    )


def detection() -> dict[str, Any]:
    panels = [
        stat(
            "Spikes started (1 h)",
            "sum(increase(wikipulse_spikes_started_total[1h]))",
            "Spikes the detector started in the last hour of wall-clock time.",
            decimals=0,
        ),
        stat(
            "Active spikes",
            "sum(wikipulse_spikes_active)",
            "Spikes currently active in the detector.",
        ),
        stat(
            "Detector event time",
            "max(wikipulse_detector_last_event_timestamp_seconds) * 1000",
            "The detector's watermark: the event time of the newest edit it processed. "
            "During a replay this is the recording's time, not today.",
            unit="dateTimeAsIso",
        ),
        stat(
            "Open events",
            "sum(wikipulse_grouper_active_events)",
            "Events the grouper keeps open (new spikes may still join them).",
        ),
        timeseries(
            "Spikes started per 10 minutes, by window",
            [
                Query(
                    "sum by (window) (increase(wikipulse_spikes_started_total[10m]))",
                    "{{window}} window",
                )
            ],
            "Which start rule fired: the short window (a sudden burst) or the long window "
            "(a story that builds up over an hour).",
        ),
        timeseries(
            "Active spikes",
            [Query("sum(wikipulse_spikes_active)", "active")],
            "Spikes active over time.",
        ),
        timeseries(
            "Edits in and out of the detector",
            [
                Query(
                    'sum(rate(wikipulse_consumer_messages_total{outcome="kept"}[1m]))', "written"
                ),
                Query("sum(rate(wikipulse_detector_entries_total[1m]))", "detected"),
            ],
            "Edits written to the stream and edits the detector processed, per second. "
            "Apart for long means the detector falls behind.",
            unit="suffix:/s",
        ),
        timeseries(
            "Spikes grouped into events",
            [
                Query(
                    "sum by (action) (increase(wikipulse_grouper_spikes_total[10m]))",
                    "{{action}}",
                )
            ],
            "Per 10 minutes: spikes that created a new event, or joined an existing one.",
        ),
    ]
    return dashboard(
        "wikipulse-detection",
        "WikiPulse: Detection",
        "Spikes and events (stages 2 and 3).",
        panels,
    )


def quantile(q: float, metric: str, by: str = "le, model", window: str = "5m") -> str:
    return f"histogram_quantile({q}, sum by ({by}) (rate({metric}_bucket[{window}])))"


FAILURE_RATE = (
    'sum(increase(wikipulse_extractions_total{outcome="failed"}[30m]))'
    " / sum(increase(wikipulse_extractions_total[30m]))"
)


def llm_usage() -> dict[str, Any]:
    latency = "wikipulse_llm_request_duration_seconds"
    panels = [
        stat(
            "LLM requests (1 h)",
            "sum(increase(wikipulse_llm_requests_total[1h]))",
            "Requests to the LLM from every service (extraction, questions, agent).",
            decimals=0,
        ),
        stat(
            "LLM latency p95 (1 h)",
            quantile(0.95, latency, by="le", window="1h"),
            "95% of LLM requests took less than this (estimated from histogram buckets).",
            unit="s",
            thresholds=steps((GREEN, None), (YELLOW, 30), (RED, 120)),
        ),
        stat(
            "Tokens (1 h)",
            "sum(increase(wikipulse_llm_tokens_total[1h]))",
            "Tokens read and generated by the LLM.",
            decimals=0,
        ),
        stat(
            "Extraction failure rate (30 min)",
            FAILURE_RATE,
            "Share of graph extractions that failed. Alert above 25% (with 4 or more).",
            unit="percentunit",
            thresholds=steps((GREEN, None), (YELLOW, 0.1), (RED, 0.25)),
        ),
        timeseries(
            "LLM requests per minute, by model and outcome",
            [
                Query(
                    "sum by (model, outcome) (rate(wikipulse_llm_requests_total[5m])) * 60",
                    "{{model}} {{outcome}}",
                )
            ],
            "Requests per minute; errors include refused requests and timeouts.",
        ),
        timeseries(
            "LLM latency, by model",
            [
                Query(quantile(0.5, latency), "p50 {{model}}"),
                Query(quantile(0.95, latency), "p95 {{model}}"),
            ],
            "Median and 95th percentile of the time per request, model loading included.",
            unit="s",
        ),
        timeseries(
            "Tokens per minute",
            [
                Query(
                    "sum by (model, direction) (rate(wikipulse_llm_tokens_total[5m])) * 60",
                    "{{model}} {{direction}}",
                )
            ],
            "Input (prompt) and output (generated, thinking included) tokens.",
        ),
        timeseries(
            "Graph extractions, by outcome",
            [
                Query(
                    "sum by (outcome) (increase(wikipulse_extractions_total[10m]))",
                    "{{outcome}}",
                ),
                Query(
                    quantile(0.95, "wikipulse_extraction_duration_seconds", by="le"),
                    "p95 seconds",
                ),
            ],
            "Per 10 minutes. Failed events are retried later (graph_extractions table).",
        ),
        timeseries(
            "Questions (POST /ask), by outcome",
            [
                Query(
                    "sum by (outcome) (increase(wikipulse_api_questions_total[10m]))",
                    "{{outcome}}",
                ),
                Query(
                    quantile(0.95, "wikipulse_api_question_duration_seconds", by="le"),
                    "p95 seconds",
                ),
            ],
            "Questions answered through the API, per 10 minutes; busy = refused (429).",
            width=24,
        ),
    ]
    return dashboard(
        "wikipulse-llm",
        "WikiPulse: LLM usage",
        "Requests, latency and tokens of the LLM; graph extraction and questions (stages 4 and 5).",
        panels,
    )


DRIFT_LEGEND = "v{{model_version}} {{run_id}}"


def model_health() -> dict[str, Any]:
    by_report = "max by (model_name, model_version, run_id)"
    panels = [
        stat(
            "Drift status (newest report per run)",
            f"{by_report} (wikipulse_drift_detected)",
            "From the weekly drift check's newest report per detector run (drift_reports).",
            legend=DRIFT_LEGEND,
            width=12,
            mappings=OK_DRIFT,
            thresholds=steps((GREEN, None), (RED, 1)),
        ),
        stat(
            "Reports readable",
            "max(wikipulse_model_health_up)",
            "Whether the API could read the drift reports and scores from Postgres.",
            mappings=UP_DOWN,
            thresholds=steps((RED, None), (GREEN, 1)),
        ),
        stat(
            "Report period end",
            f"{by_report} (wikipulse_drift_report_period_end_timestamp_seconds) * 1000",
            "End of each newest report's period.",
            legend=DRIFT_LEGEND,
            unit="dateTimeAsIso",
        ),
        bars(
            "Score PSI and its noise floor",
            [
                Query(f"{by_report} (wikipulse_drift_score_psi)", "PSI " + DRIFT_LEGEND),
                Query(f"{by_report} (wikipulse_drift_score_noise_floor)", "floor " + DRIFT_LEGEND),
            ],
            "Population stability index of the score against the training data. It counts as "
            "drift only above both 0.2 and the floor (what chance alone reaches).",
            decimals=3,
        ),
        bars(
            "Most shifted features (PSI)",
            [
                Query(
                    "topk(10, max by (feature, run_id) (wikipulse_drift_feature_psi))",
                    "{{feature}} ({{run_id}})",
                )
            ],
            "The ten largest feature PSIs over the newest reports.",
            decimals=2,
            thresholds=steps((GREEN, None), (YELLOW, 0.1), (RED, 0.25)),
        ),
        stat(
            "Drifted features",
            f"{by_report} (wikipulse_drift_features_drifted)",
            "Features whose PSI passed the threshold and the noise floor (5 or more = drift).",
            legend=DRIFT_LEGEND,
            width=8,
            thresholds=steps((GREEN, None), (YELLOW, 1), (RED, 5)),
        ),
        stat(
            "Stored scores",
            f"{by_report} (wikipulse_model_stored_scores)",
            "Spikes scored, in spike_scores (live scorer and backfills).",
            legend=DRIFT_LEGEND,
            width=8,
        ),
        stat(
            "News share of stored scores",
            f"{by_report} (wikipulse_model_stored_news_share)",
            "Share of scored spikes the model classified as news.",
            legend=DRIFT_LEGEND,
            width=8,
            unit="percentunit",
        ),
        timeseries(
            "Live scores per hour, by verdict",
            [
                Query(
                    "sum by (model_version, is_news) "
                    "(increase(wikipulse_model_scored_spikes_total[1h]))",
                    "v{{model_version}} news={{is_news}}",
                )
            ],
            "Spikes the live scorer (wikipulse.ml.serve) scored.",
        ),
        bars(
            "Live score distribution (6 h)",
            [
                Query(
                    "sum by (le) (increase(wikipulse_model_score_bucket[6h]))",
                    "score <= {{le}}",
                )
            ],
            "Cumulative count of live scores per bucket: how many scored at most 0.1, 0.2, ...",
            decimals=0,
        ),
    ]
    return dashboard(
        "wikipulse-model",
        "WikiPulse: Model health",
        "The news classifier: drift reports and scores (stage 7).",
        panels,
    )


DASHBOARDS = {
    "pipeline-health.json": pipeline_health,
    "detection.json": detection,
    "llm-usage.json": llm_usage,
    "model-health.json": model_health,
}


def render(builder: Any) -> str:
    return json.dumps(builder(), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only check the files are current")
    args = parser.parse_args(argv)
    stale = []
    for name, builder in DASHBOARDS.items():
        path = OUT / name
        text = render(builder)
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == text:
            continue
        if args.check:
            stale.append(name)
        else:
            OUT.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
            print(f"Wrote {path.relative_to(ROOT)}")
    if stale:
        print(f"Out of date: {', '.join(stale)}; run uv run python scripts/dashboards.py")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

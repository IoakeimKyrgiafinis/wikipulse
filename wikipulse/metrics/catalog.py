"""Every WikiPulse metric in one place: name, type, labels and why it exists.

The three types, and when each fits:

- **Counter**: a number that only goes up (messages read, spikes started, tokens used).
  Prometheus stores the running total; `rate(x_total[1m])` turns it into "per second".
  A restart resets it to 0, which `rate()` recognizes and handles. Never use a counter
  for something that can go down.
- **Gauge**: a value that goes up and down (lag, queue length, active spikes). Prometheus
  stores what it reads at each scrape; nothing happens between scrapes.
- **Histogram**: a distribution (how long LLM calls take). Every observation lands in a
  bucket (`le="5"` = at most 5 s), and `histogram_quantile(0.95, ...)` estimates a
  percentile from the bucket counts across all pods. Buckets are chosen up front, from
  what the values usually are.

Naming follows Prometheus' conventions: `wikipulse_` first, the unit last
(`_seconds`, `_bytes`), and counters end in `_total` (the client library adds it).
Labels split a metric into series (`outcome="kept"`); keep their values few and fixed
(never a page title or a user): every combination is a series of its own.

The metrics live in the client library's default registry. A process exports only the
metrics of the modules it imports, so most services show a few unused metrics at 0;
dashboards sum or take the maximum, which zeros don't disturb.
"""

from prometheus_client import Counter, Gauge, Histogram, disable_created_metrics


def start_at_zero(metric: Counter, *values: str) -> None:
    """Export these label values at 0 right away, before their first increment.

    A labeled series only exists once it is used. Without this, a counter's first
    increment would make it *appear* at 1, and `rate()`/`increase()` would miss that
    first event (they need two samples); a panel would also show "No data" instead of 0.
    Called by the process that owns the metric (e.g. the detector for spikes started),
    so other services don't export zeros for metrics they never touch.
    """
    for value in values:
        metric.labels(value)


# Without this, every counter also exports a `_created` timestamp series, which nothing here
# uses: half the series for no benefit.
disable_created_metrics()

# --- stage 1: the stream consumer (live) and the replay writer ---

CONSUMER_MESSAGES = Counter(
    "wikipulse_consumer_messages",
    "Messages the consumer handled, by outcome: kept (written to the stream), duplicates, "
    "discarded (not an English article edit), malformed.",
    ["outcome"],
)
CONSUMER_RECONNECTS = Counter(
    "wikipulse_consumer_reconnects",
    "Times the consumer lost the Wikimedia stream and connected again.",
)
CONSUMER_LAST_EVENT = Gauge(
    "wikipulse_consumer_last_event_timestamp_seconds",
    "Event time (Unix seconds) of the newest edit written to the stream.",
)
CONSUMER_LAG = Gauge(
    "wikipulse_consumer_lag_seconds",
    "How far the newest written edit is behind 'now': the wall clock for the live "
    "consumer, the replay's own event-time clock for a replay. Computed at scrape time.",
)

# --- stage 2: spike detection ---

DETECTOR_ENTRIES = Counter(
    "wikipulse_detector_entries",
    "Stream entries the detector applied to its state.",
)
DETECTOR_LAST_EVENT = Gauge(
    "wikipulse_detector_last_event_timestamp_seconds",
    "Event time (Unix seconds) of the newest edit the detector processed (its watermark).",
)
DETECTOR_STREAM_LAG = Gauge(
    "wikipulse_detector_stream_lag_entries",
    "Entries in the edit stream not yet delivered to the detector's consumer group "
    "(the queue in front of the detector).",
)
DETECTOR_PENDING = Gauge(
    "wikipulse_detector_pending_entries",
    "Entries delivered to the detector but not yet acknowledged (until the next snapshot).",
)
SPIKES_STARTED = Counter(
    "wikipulse_spikes_started",
    "Spikes started, by the window whose rule fired (short or long).",
    ["window"],
)
SPIKES_ACTIVE = Gauge(
    "wikipulse_spikes_active",
    "Spikes currently active in the detector.",
)

# --- stage 3: enrichment (Wikimedia API) and event grouping ---

WIKI_API_REQUESTS = Counter(
    "wikipulse_wiki_api_requests",
    "Wikimedia API answers by HTTP status ('cache' = answered from the cache, "
    "'error' = no answer). Watch 429: Wikimedia asking us to slow down.",
    ["status"],
)
GROUPER_SPIKES = Counter(
    "wikipulse_grouper_spikes",
    "Spikes the grouper placed into events, by whether they created a new event.",
    ["action"],
)
GROUPER_ACTIVE_EVENTS = Gauge(
    "wikipulse_grouper_active_events",
    "Events still open in the grouper.",
)

# --- stage 4: the LLM and graph extraction ---

LLM_REQUESTS = Counter(
    "wikipulse_llm_requests",
    "LLM requests by backend, model and outcome (ok or error).",
    ["backend", "model", "outcome"],
)
LLM_LATENCY = Histogram(
    "wikipulse_llm_request_duration_seconds",
    "Time one LLM request took, model loading included.",
    ["backend", "model"],
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300),
)
LLM_TOKENS = Counter(
    "wikipulse_llm_tokens",
    "Tokens sent to (input) and generated by (output) the LLM.",
    ["backend", "model", "direction"],
)
EXTRACTIONS = Counter(
    "wikipulse_extractions",
    "Graph extractions of one event, by outcome (ok or failed).",
    ["outcome"],
)
EXTRACTION_DURATION = Histogram(
    "wikipulse_extraction_duration_seconds",
    "Time one event's graph extraction took (pages, LLM, linking, Neo4j).",
    buckets=(1, 5, 10, 20, 30, 60, 120, 300, 600),
)

# --- stage 5: the API ---

QUESTIONS = Counter(
    "wikipulse_api_questions",
    "Questions asked through POST /ask, by outcome (ok, busy, invalid, unavailable, error).",
    ["outcome"],
)
QUESTION_DURATION = Histogram(
    "wikipulse_api_question_duration_seconds",
    "Time to answer one question (retrieval and the LLM).",
    buckets=(1, 2, 5, 10, 20, 30, 60, 120),
)
WEBSOCKET_CLIENTS = Gauge(
    "wikipulse_api_websocket_clients",
    "Browsers connected to the live page.",
)

# --- stage 6: the data lake ---

ARCHIVER_EDITS = Counter(
    "wikipulse_archiver_edits_archived",
    "Edits written to the data lake (and then acknowledged in the stream).",
)
ARCHIVER_FAILURES = Counter(
    "wikipulse_archiver_flush_failures",
    "Failed writes to the lake (the edits stay buffered and are retried).",
)

# --- stage 7: the news classifier (live scorer) ---

MODEL_SCORES = Histogram(
    "wikipulse_model_score",
    "Scores the news classifier gave live spikes (0..1, higher = more likely news).",
    ["model_version"],
    buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)
MODEL_SCORED = Counter(
    "wikipulse_model_scored_spikes",
    "Spikes the live scorer scored, by whether they were classified as news.",
    ["model_version", "is_news"],
)
